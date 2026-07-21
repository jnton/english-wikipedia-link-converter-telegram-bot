import json

from aws_setup.common import (
    MONITOR_FUNCTION,
    MONITOR_ROLE,
    SCHEDULER_ROLE,
    ensure_role,
)


def put_main_execution_policy(
    iam,
    role_name: str,
    queue_arn: str,
    dlq_arn: str,
    table_arn: str,
    topic_arn: str,
) -> None:
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "sqs:SendMessage",
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:ChangeMessageVisibility",
                    "sqs:GetQueueAttributes",
                ],
                "Resource": [queue_arn, dlq_arn],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                "Resource": table_arn,
            },
            {
                "Effect": "Allow",
                "Action": "sns:Publish",
                "Resource": topic_arn,
            },
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="ToEnWikipediaBotRuntime",
        PolicyDocument=json.dumps(policy),
    )


def put_monitor_policy(
    iam,
    queue_arn: str,
    dlq_arn: str,
    table_arn: str,
    topic_arn: str,
    account_id: str,
    region: str,
) -> str:
    role_arn = ensure_role(
        iam,
        MONITOR_ROLE,
        "lambda.amazonaws.com",
        "Runtime role for the independent ToEnWikipediaBot monitor",
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws/lambda/{MONITOR_FUNCTION}:*",
            },
            {
                "Effect": "Allow",
                "Action": "sqs:GetQueueAttributes",
                "Resource": [queue_arn, dlq_arn],
            },
            {
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
                "Resource": table_arn,
            },
            {
                "Effect": "Allow",
                "Action": "sns:Publish",
                "Resource": topic_arn,
            },
            {
                "Effect": "Allow",
                "Action": "cloudwatch:GetMetricStatistics",
                "Resource": "*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName=MONITOR_ROLE,
        PolicyName="ToEnWikipediaBotMonitorRuntime",
        PolicyDocument=json.dumps(policy),
    )
    return role_arn


def put_scheduler_policy(iam, monitor_arn: str) -> str:
    role_arn = ensure_role(
        iam,
        SCHEDULER_ROLE,
        "scheduler.amazonaws.com",
        "Allows EventBridge Scheduler to invoke the ToEnWikipediaBot monitor",
    )
    iam.put_role_policy(
        RoleName=SCHEDULER_ROLE,
        PolicyName="InvokeToEnWikipediaBotMonitor",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "lambda:InvokeFunction",
                        "Resource": monitor_arn,
                    }
                ],
            }
        ),
    )
    return role_arn
