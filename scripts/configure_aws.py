import json
import os
import secrets
from pathlib import Path

from aws_setup.common import (
    DLQ_NAME,
    MONITOR_FUNCTION,
    QUEUE_NAME,
    SCHEDULE_NAME,
    STATE_TABLE,
    WORKER_FUNCTION,
    client,
    role_name_from_arn,
)
from aws_setup.iam_runtime import (
    put_main_execution_policy,
    put_monitor_policy,
    put_scheduler_policy,
)
from aws_setup.lambda_resources import (
    disable_legacy_api_gateway_invocation,
    ensure_event_source,
    ensure_function_url,
    ensure_monitor_function,
    ensure_worker_function,
    update_main_function,
)
from aws_setup.monitoring import (
    ensure_alarms,
    ensure_budget,
    ensure_log_retention,
    ensure_schedule,
)
from aws_setup.storage import ensure_queues, ensure_table, ensure_topic
from aws_setup.telegram_setup import configure_webhook


def main() -> None:
    region = os.environ["AWS_REGION"]
    function_name = os.getenv("LAMBDA_FUNCTION_NAME", "ToEnWikipediaBot")
    bot_zip = Path(os.getenv("BOT_ZIP_PATH", "package.zip"))
    monitor_zip = Path(os.getenv("MONITOR_ZIP_PATH", "monitor-package.zip"))
    for package in (bot_zip, monitor_zip):
        if not package.exists():
            raise FileNotFoundError(package)

    sts = client("sts", region)
    lambda_client = client("lambda", region)
    sqs = client("sqs", region)
    dynamodb = client("dynamodb", region)
    sns = client("sns", region)
    iam = client("iam")
    scheduler = client("scheduler", region)
    cloudwatch = client("cloudwatch", region)
    logs = client("logs", region)

    account_id = sts.get_caller_identity()["Account"]
    alert_email = os.getenv("ALERT_EMAIL", "").strip()
    budget_amount = os.getenv("MONTHLY_BUDGET_USD", "1").strip() or "1"

    queues = ensure_queues(sqs)
    table_arn = ensure_table(dynamodb, region, account_id)
    topic_arn = ensure_topic(sns, alert_email)

    function_url = ensure_function_url(lambda_client, function_name)
    current = lambda_client.get_function_configuration(FunctionName=function_name)
    current_environment = current.get("Environment", {}).get("Variables", {})
    token = current_environment.get("TELEGRAM_BOT_TOKEN") or current_environment.get(
        "YOUR_TELEGRAM_BOT_TOKEN"
    )
    if not token:
        raise RuntimeError("The Lambda function has no Telegram bot token environment variable")

    webhook_secret = current_environment.get("TELEGRAM_WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    deployment_version = os.getenv("GITHUB_SHA", "unknown")
    optional_runtime_values = {
        key: os.getenv(key, "").strip()
        for key in [
            "ADMIN_CHAT_ID",
            "STATUS_CHANNEL_ID",
            "STATUS_CHANNEL_URL",
            "STATUS_BOT_TOKEN",
        ]
    }

    shared_environment = {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_WEBHOOK_SECRET": webhook_secret,
        "SQS_QUEUE_URL": queues["queue_url"],
        "DLQ_URL": queues["dlq_url"],
        "STATE_TABLE_NAME": STATE_TABLE,
        "ALERT_TOPIC_ARN": topic_arn,
        "FUNCTION_URL": function_url,
        "DEPLOYMENT_VERSION": deployment_version,
        "RECOVERY_NOTICES_ENABLED": "true",
        **optional_runtime_values,
    }

    main_role_name = role_name_from_arn(current["Role"])
    put_main_execution_policy(
        iam,
        main_role_name,
        queues["queue_arn"],
        queues["dlq_arn"],
        table_arn,
        topic_arn,
    )
    main_config = update_main_function(lambda_client, function_name, shared_environment)
    worker_arn = ensure_worker_function(
        lambda_client,
        bot_zip,
        main_config["Role"],
        shared_environment,
    )

    monitor_role_arn = put_monitor_policy(
        iam,
        queues["queue_arn"],
        queues["dlq_arn"],
        table_arn,
        topic_arn,
        account_id,
        region,
    )
    monitor_arn = ensure_monitor_function(
        lambda_client,
        monitor_zip,
        monitor_role_arn,
        shared_environment,
    )

    ensure_event_source(lambda_client, worker_arn, queues["queue_arn"])
    scheduler_role_arn = put_scheduler_policy(iam, monitor_arn)
    ensure_schedule(scheduler, monitor_arn, scheduler_role_arn)
    ensure_alarms(cloudwatch, function_name, topic_arn)
    ensure_log_retention(logs, [function_name, WORKER_FUNCTION, MONITOR_FUNCTION])
    ensure_budget(account_id, alert_email, budget_amount)

    webhook_info = configure_webhook(token, function_url, webhook_secret)
    disable_legacy_api_gateway_invocation(lambda_client, function_name)
    print(
        json.dumps(
            {
                "function_url": function_url,
                "queue": QUEUE_NAME,
                "dead_letter_queue": DLQ_NAME,
                "state_table": STATE_TABLE,
                "worker_function": WORKER_FUNCTION,
                "monitor_function": MONITOR_FUNCTION,
                "health_schedule": f"{SCHEDULE_NAME}: rate(15 minutes)",
                "pending_telegram_updates": webhook_info.get("pending_update_count", 0),
                "alert_email_configured": bool(alert_email),
                "status_channel_configured": bool(optional_runtime_values["STATUS_CHANNEL_ID"]),
                "monthly_budget_usd": budget_amount if alert_email else None,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
