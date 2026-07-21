import json

from botocore.exceptions import ClientError

from aws_setup.common import APPLICATION, DLQ_NAME, QUEUE_NAME, STATE_TABLE, TOPIC_NAME


def ensure_queue(sqs, name: str, attributes: dict[str, str]) -> tuple[str, str]:
    try:
        queue_url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "AWS.SimpleQueueService.NonExistentQueue",
            "QueueDoesNotExist",
        }:
            raise
        queue_url = sqs.create_queue(
            QueueName=name,
            Attributes=attributes,
            tags={"Application": APPLICATION},
        )["QueueUrl"]
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes=attributes)
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["QueueArn"],
    )["Attributes"]["QueueArn"]
    return queue_url, queue_arn


def ensure_queues(sqs) -> dict[str, str]:
    dlq_url, dlq_arn = ensure_queue(
        sqs,
        DLQ_NAME,
        {
            "MessageRetentionPeriod": "1209600",
            "ReceiveMessageWaitTimeSeconds": "20",
            "SqsManagedSseEnabled": "true",
        },
    )
    queue_url, queue_arn = ensure_queue(
        sqs,
        QUEUE_NAME,
        {
            "VisibilityTimeout": "90",
            "MessageRetentionPeriod": "172800",
            "ReceiveMessageWaitTimeSeconds": "20",
            "SqsManagedSseEnabled": "true",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}
            ),
        },
    )
    return {
        "queue_url": queue_url,
        "queue_arn": queue_arn,
        "dlq_url": dlq_url,
        "dlq_arn": dlq_arn,
    }


def ensure_table(dynamodb, region: str, account_id: str) -> str:
    try:
        dynamodb.describe_table(TableName=STATE_TABLE)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        dynamodb.create_table(
            TableName=STATE_TABLE,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            SSESpecification={"Enabled": True},
            Tags=[{"Key": "Application", "Value": APPLICATION}],
        )
        dynamodb.get_waiter("table_exists").wait(TableName=STATE_TABLE)

    ttl = dynamodb.describe_time_to_live(TableName=STATE_TABLE).get(
        "TimeToLiveDescription", {}
    )
    if ttl.get("TimeToLiveStatus") in {"DISABLED", None}:
        dynamodb.update_time_to_live(
            TableName=STATE_TABLE,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
        )
    return f"arn:aws:dynamodb:{region}:{account_id}:table/{STATE_TABLE}"


def ensure_topic(sns, alert_email: str) -> str:
    topic_arn = sns.create_topic(
        Name=TOPIC_NAME,
        Tags=[{"Key": "Application", "Value": APPLICATION}],
    )["TopicArn"]
    if alert_email:
        subscriptions = sns.list_subscriptions_by_topic(TopicArn=topic_arn).get(
            "Subscriptions", []
        )
        exists = any(
            subscription.get("Protocol") == "email"
            and subscription.get("Endpoint", "").lower() == alert_email.lower()
            for subscription in subscriptions
        )
        if not exists:
            sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=alert_email)
    return topic_arn
