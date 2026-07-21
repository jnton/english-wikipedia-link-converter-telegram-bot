import json
import urllib.request

import boto3
from botocore.exceptions import ClientError

APPLICATION = "ToEnWikipediaBot"
STATE_TABLE = "ToEnWikipediaBotState"
QUEUE_NAME = "ToEnWikipediaBot-updates"
DLQ_NAME = "ToEnWikipediaBot-dlq"
TOPIC_NAME = "ToEnWikipediaBot-alerts"
MONITOR_FUNCTION = "ToEnWikipediaBotMonitor"
MONITOR_ROLE = "ToEnWikipediaBotMonitorRole"
SCHEDULER_ROLE = "ToEnWikipediaBotSchedulerRole"
SCHEDULE_NAME = "ToEnWikipediaBot-health-check"


def client(service: str, region: str | None = None):
    return boto3.client(service, region_name=region) if region else boto3.client(service)


def role_name_from_arn(role_arn: str) -> str:
    return role_arn.rsplit("/", 1)[-1]


def ensure_role(iam, name: str, service: str, description: str) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": service},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=name)["Role"]
        iam.update_assume_role_policy(
            RoleName=name,
            PolicyDocument=json.dumps(trust),
        )
        return role["Arn"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            raise
        role = iam.create_role(
            RoleName=name,
            Description=description,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[{"Key": "Application", "Value": APPLICATION}],
        )["Role"]
        return role["Arn"]


def telegram_api(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description", "Telegram API failure")))
    return data.get("result", {})
