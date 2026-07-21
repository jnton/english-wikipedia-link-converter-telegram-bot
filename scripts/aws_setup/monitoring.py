import json

from botocore.exceptions import ClientError

from aws_setup.common import (
    APPLICATION,
    DLQ_NAME,
    MONITOR_FUNCTION,
    QUEUE_NAME,
    SCHEDULE_NAME,
    WORKER_FUNCTION,
    client,
)


def ensure_schedule(scheduler, monitor_arn: str, role_arn: str) -> None:
    request = {
        "Name": SCHEDULE_NAME,
        "ScheduleExpression": "rate(15 minutes)",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": "ENABLED",
        "Description": "Checks ToEnWikipediaBot without depending on the bot Lambda",
        "Target": {
            "Arn": monitor_arn,
            "RoleArn": role_arn,
            "Input": json.dumps(
                {
                    "source": "aws.scheduler",
                    "detail-type": "ToEnWikipediaBotHealthCheck",
                }
            ),
            "RetryPolicy": {
                "MaximumEventAgeInSeconds": 900,
                "MaximumRetryAttempts": 2,
            },
        },
    }
    try:
        scheduler.get_schedule(Name=SCHEDULE_NAME)
        scheduler.update_schedule(**request)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        scheduler.create_schedule(**request)


def put_alarm(
    cloudwatch,
    name: str,
    namespace: str,
    metric: str,
    dimensions: list[dict[str, str]],
    threshold: float,
    period: int,
    evaluation_periods: int,
    topic_arn: str,
    statistic: str = "Sum",
) -> None:
    cloudwatch.put_metric_alarm(
        AlarmName=name,
        AlarmDescription=f"Managed monitoring for {APPLICATION}",
        ActionsEnabled=True,
        AlarmActions=[topic_arn],
        MetricName=metric,
        Namespace=namespace,
        Statistic=statistic,
        Dimensions=dimensions,
        Period=period,
        EvaluationPeriods=evaluation_periods,
        DatapointsToAlarm=1,
        Threshold=threshold,
        ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="notBreaching",
        Tags=[{"Key": "Application", "Value": APPLICATION}],
    )


def ensure_alarms(cloudwatch, function_name: str, topic_arn: str) -> None:
    function_dimensions = [{"Name": "FunctionName", "Value": function_name}]
    worker_dimensions = [{"Name": "FunctionName", "Value": WORKER_FUNCTION}]
    monitor_dimensions = [{"Name": "FunctionName", "Value": MONITOR_FUNCTION}]
    queue_dimensions = [{"Name": "QueueName", "Value": QUEUE_NAME}]
    dlq_dimensions = [{"Name": "QueueName", "Value": DLQ_NAME}]

    alarms = [
        (
            "ToEnWikipediaBot-FunctionUrl5xx",
            "AWS/Lambda",
            "Url5xxCount",
            function_dimensions,
            1,
            300,
            1,
            "Sum",
        ),
        (
            "ToEnWikipediaBot-LambdaErrors",
            "AWS/Lambda",
            "Errors",
            function_dimensions,
            1,
            300,
            1,
            "Sum",
        ),
        (
            "ToEnWikipediaBot-WorkerErrors",
            "AWS/Lambda",
            "Errors",
            worker_dimensions,
            1,
            300,
            1,
            "Sum",
        ),
        (
            "ToEnWikipediaBot-LambdaThrottles",
            "AWS/Lambda",
            "Throttles",
            function_dimensions,
            1,
            300,
            1,
            "Sum",
        ),
        (
            "ToEnWikipediaBot-QueueAge",
            "AWS/SQS",
            "ApproximateAgeOfOldestMessage",
            queue_dimensions,
            120,
            300,
            1,
            "Maximum",
        ),
        (
            "ToEnWikipediaBot-DeadLetterQueue",
            "AWS/SQS",
            "ApproximateNumberOfMessagesVisible",
            dlq_dimensions,
            1,
            300,
            1,
            "Maximum",
        ),
        (
            "ToEnWikipediaBot-MonitorErrors",
            "AWS/Lambda",
            "Errors",
            monitor_dimensions,
            1,
            900,
            1,
            "Sum",
        ),
    ]
    managed_names = [alarm[0] for alarm in alarms]
    existing_names: set[str] = set()
    paginator = cloudwatch.get_paginator("describe_alarms")
    for page in paginator.paginate():
        existing_names.update(
            alarm["AlarmName"] for alarm in page.get("MetricAlarms", [])
        )
    other_alarm_count = len(existing_names.difference(managed_names))
    allowed_managed = max(0, 10 - other_alarm_count)

    for alarm in alarms[:allowed_managed]:
        put_alarm(cloudwatch, *alarm[:-1], topic_arn, statistic=alarm[-1])

    excess_names = [
        name for name in managed_names[allowed_managed:] if name in existing_names
    ]
    if excess_names:
        cloudwatch.delete_alarms(AlarmNames=excess_names)
    if allowed_managed < len(alarms):
        print(
            f"Skipped {len(alarms) - allowed_managed} bot alarm(s) to keep the "
            "account at or below 10 standard CloudWatch alarms."
        )


def ensure_log_retention(logs, function_names: list[str]) -> None:
    for function_name in function_names:
        group = f"/aws/lambda/{function_name}"
        try:
            logs.create_log_group(
                logGroupName=group,
                tags={"Application": APPLICATION},
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                raise
        logs.put_retention_policy(logGroupName=group, retentionInDays=14)


def ensure_budget(account_id: str, alert_email: str, amount: str) -> None:
    if not alert_email:
        return
    budgets = client("budgets", "us-east-1")
    budget_name = f"{APPLICATION}-monthly-{amount}USD"
    try:
        budgets.describe_budget(AccountId=account_id, BudgetName=budget_name)
        return
    except ClientError as error:
        if error.response["Error"]["Code"] != "NotFoundException":
            raise

    existing_budgets = budgets.describe_budgets(
        AccountId=account_id,
        MaxResults=100,
    ).get("Budgets", [])
    if len(existing_budgets) >= 2:
        print(
            "Skipped the bot budget because the account already has two budgets; "
            "this avoids creating a potentially billable additional budget."
        )
        return

    subscriber = {"SubscriptionType": "EMAIL", "Address": alert_email}
    budgets.create_budget(
        AccountId=account_id,
        Budget={
            "BudgetName": budget_name,
            "BudgetLimit": {"Amount": amount, "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
        },
        NotificationsWithSubscribers=[
            {
                "Notification": {
                    "NotificationType": "ACTUAL",
                    "ComparisonOperator": "GREATER_THAN",
                    "Threshold": 50.0,
                    "ThresholdType": "PERCENTAGE",
                },
                "Subscribers": [subscriber],
            },
            {
                "Notification": {
                    "NotificationType": "ACTUAL",
                    "ComparisonOperator": "GREATER_THAN",
                    "Threshold": 90.0,
                    "ThresholdType": "PERCENTAGE",
                },
                "Subscribers": [subscriber],
            },
            {
                "Notification": {
                    "NotificationType": "FORECASTED",
                    "ComparisonOperator": "GREATER_THAN",
                    "Threshold": 100.0,
                    "ThresholdType": "PERCENTAGE",
                },
                "Subscribers": [subscriber],
            },
        ],
    )
