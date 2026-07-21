import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 5
PENDING_UPDATE_THRESHOLD = int(os.getenv("HEALTH_PENDING_UPDATE_THRESHOLD", "10"))
QUEUE_AGE_THRESHOLD = int(os.getenv("HEALTH_QUEUE_AGE_THRESHOLD", "120"))
RECENT_WEBHOOK_ERROR_SECONDS = int(os.getenv("HEALTH_RECENT_ERROR_SECONDS", "1200"))
UNHEALTHY_REMINDER_SECONDS = int(os.getenv("HEALTH_REMINDER_SECONDS", "21600"))

_clients: dict[str, Any] = {}


def _client(service_name: str):
    client = _clients.get(service_name)
    if client is None:
        import boto3

        client = boto3.client(service_name)
        _clients[service_name] = client
    return client


def _main_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("YOUR_TELEGRAM_BOT_TOKEN")


def _notification_token() -> str | None:
    return os.getenv("STATUS_BOT_TOKEN") or _main_token()


def _http_json(url: str, payload: dict | None = None) -> dict:
    body = None
    headers = {"User-Agent": "ToEnWikipediaBotMonitor/1.0"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _telegram(
    method: str,
    payload: dict | None = None,
    *,
    token: str | None = None,
) -> dict:
    selected_token = token or _main_token()
    if not selected_token:
        raise RuntimeError("Telegram token is missing")
    data = _http_json(
        f"https://api.telegram.org/bot{selected_token}/{method}",
        payload,
    )
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description", "Telegram API failure")))
    result = data.get("result")
    return result if isinstance(result, dict) else {"result": result}


def _queue_attributes(queue_url: str | None) -> dict[str, int]:
    if not queue_url:
        return {"visible": 0, "in_flight": 0, "age": 0}
    attributes = _client("sqs").get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    ).get("Attributes", {})

    queue_name = queue_url.rstrip("/").rsplit("/", 1)[-1]
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=20)
    metric = _client("cloudwatch").get_metric_statistics(
        Namespace="AWS/SQS",
        MetricName="ApproximateAgeOfOldestMessage",
        Dimensions=[{"Name": "QueueName", "Value": queue_name}],
        StartTime=start_time,
        EndTime=end_time,
        Period=300,
        Statistics=["Maximum"],
    )
    datapoints = metric.get("Datapoints", [])
    age = int(max((point.get("Maximum", 0) for point in datapoints), default=0))
    return {
        "visible": int(attributes.get("ApproximateNumberOfMessages", "0")),
        "in_flight": int(attributes.get("ApproximateNumberOfMessagesNotVisible", "0")),
        "age": age,
    }


def collect_health() -> dict[str, Any]:
    now = int(time.time())
    reasons: list[str] = []

    function_url = os.getenv("FUNCTION_URL", "")
    endpoint_ok = False
    endpoint_version = "unknown"
    try:
        endpoint = _http_json(function_url.rstrip("/") + "/health")
        endpoint_ok = (
            endpoint.get("status") == "ok"
            and endpoint.get("queue_configured") is True
        )
        endpoint_version = str(endpoint.get("version", "unknown"))
        if not endpoint_ok:
            reasons.append("Function URL is reachable but queue configuration is incomplete")
    except Exception as error:
        reasons.append(f"Function URL health check failed: {type(error).__name__}")

    webhook_info: dict[str, Any] = {}
    webhook_ok = False
    try:
        webhook_info = _telegram("getWebhookInfo")
        expected_url = function_url.rstrip("/")
        actual_url = str(webhook_info.get("url", "")).rstrip("/")
        webhook_ok = bool(actual_url) and actual_url == expected_url
        if not webhook_ok:
            reasons.append("Telegram webhook URL does not match the Lambda Function URL")
    except Exception as error:
        reasons.append(f"Telegram getWebhookInfo failed: {type(error).__name__}")

    pending_updates = int(webhook_info.get("pending_update_count", 0) or 0)
    last_error_date = int(webhook_info.get("last_error_date", 0) or 0)
    last_error_message = str(webhook_info.get("last_error_message", ""))[:300]
    if pending_updates >= PENDING_UPDATE_THRESHOLD:
        reasons.append(f"Telegram has {pending_updates} pending updates")
    if (
        last_error_date
        and now - last_error_date <= RECENT_WEBHOOK_ERROR_SECONDS
        and (pending_updates > 0 or not endpoint_ok or not webhook_ok)
    ):
        reasons.append(f"Recent Telegram webhook error: {last_error_message or 'unknown'}")

    try:
        queue = _queue_attributes(os.getenv("SQS_QUEUE_URL"))
    except Exception as error:
        queue = {"visible": 0, "in_flight": 0, "age": 0}
        reasons.append(f"SQS health check failed: {type(error).__name__}")

    try:
        dlq = _queue_attributes(os.getenv("DLQ_URL"))
    except Exception as error:
        dlq = {"visible": 0, "in_flight": 0, "age": 0}
        reasons.append(f"DLQ health check failed: {type(error).__name__}")

    if queue["age"] >= QUEUE_AGE_THRESHOLD:
        reasons.append(f"Oldest queued update is {queue['age']} seconds old")
    if dlq["visible"] > 0:
        reasons.append(f"Dead-letter queue contains {dlq['visible']} update(s)")

    healthy = endpoint_ok and webhook_ok and not reasons
    return {
        "status": "healthy" if healthy else "unhealthy",
        "endpoint_ok": endpoint_ok,
        "webhook_ok": webhook_ok,
        "checked_at": now,
        "pending_updates": pending_updates,
        "queue_depth": queue["visible"] + queue["in_flight"],
        "queue_age": queue["age"],
        "dlq_depth": dlq["visible"],
        "endpoint_version": endpoint_version,
        "reasons": reasons,
    }


def _deserialize_item(item: dict) -> dict[str, str]:
    return {key: str(next(iter(value.values()))) for key, value in item.items()}


def load_state() -> dict[str, str]:
    table_name = os.getenv("STATE_TABLE_NAME")
    if not table_name:
        return {}
    try:
        item = _client("dynamodb").get_item(
            TableName=table_name,
            Key={"pk": {"S": "monitor#state"}},
            ConsistentRead=True,
        ).get("Item", {})
        return _deserialize_item(item)
    except Exception:
        logger.exception("Could not read monitor state.")
        return {}


def save_state(health: dict[str, Any], notified_at: int) -> None:
    table_name = os.getenv("STATE_TABLE_NAME")
    if not table_name:
        return
    item = {
        "pk": {"S": "monitor#state"},
        "status": {"S": health["status"]},
        "checked_at": {"N": str(health["checked_at"])},
        "pending_updates": {"N": str(health["pending_updates"])},
        "queue_depth": {"N": str(health["queue_depth"])},
        "queue_age": {"N": str(health["queue_age"])},
        "dlq_depth": {"N": str(health["dlq_depth"])},
        "endpoint_version": {"S": str(health["endpoint_version"])[:100]},
        "notified_at": {"N": str(notified_at)},
        "reasons": {"S": " | ".join(health["reasons"])[:1000]},
    }
    _client("dynamodb").put_item(TableName=table_name, Item=item)


def _publish_sns(subject: str, message: str) -> None:
    topic_arn = os.getenv("ALERT_TOPIC_ARN")
    if not topic_arn:
        return
    _client("sns").publish(
        TopicArn=topic_arn,
        Subject=subject[:100],
        Message=message,
    )


def _send_telegram(chat_id: str, text: str) -> None:
    if not chat_id:
        return
    try:
        _telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            token=_notification_token(),
        )
    except Exception:
        logger.exception("Could not send Telegram status notification.")


def _messages(health: dict[str, Any], recovered: bool) -> tuple[str, str, str]:
    status_url = os.getenv("STATUS_CHANNEL_URL", "")
    if recovered:
        public = (
            "✅ English Wikipedia Link Converter is operational again. "
            "If a conversion was missed during the interruption, please resend the link."
        )
        subject = "ToEnWikipediaBot recovered"
    else:
        public = (
            "⚠️ English Wikipedia Link Converter is experiencing a service interruption. "
            "Older group messages may be skipped to prevent delayed spam."
        )
        subject = "ToEnWikipediaBot outage"
    if status_url:
        public += f"\n{status_url}"

    details = "\n".join(f"- {reason}" for reason in health["reasons"]) or "- none"
    admin = (
        f"{public}\n\n"
        f"Pending Telegram updates: {health['pending_updates']}\n"
        f"Queue depth: {health['queue_depth']}\n"
        f"Oldest queued update: {health['queue_age']}s\n"
        f"DLQ depth: {health['dlq_depth']}\n"
        f"Endpoint version: {health['endpoint_version']}\n"
        f"Details:\n{details}"
    )
    return subject, public, admin


def handler(event, context):
    health = collect_health()
    previous = load_state()
    now = int(time.time())
    previous_status = previous.get("status")
    last_notified = int(previous.get("notified_at", "0") or 0)

    recovered = health["status"] == "healthy" and previous_status == "unhealthy"
    new_outage = health["status"] == "unhealthy" and previous_status != "unhealthy"
    reminder = (
        health["status"] == "unhealthy"
        and previous_status == "unhealthy"
        and now - last_notified >= UNHEALTHY_REMINDER_SECONDS
    )

    notified_at = last_notified
    if recovered or new_outage or reminder:
        subject, public_message, admin_message = _messages(health, recovered)
        _publish_sns(subject, admin_message)
        _send_telegram(os.getenv("ADMIN_CHAT_ID", ""), admin_message)
        if recovered or new_outage:
            _send_telegram(os.getenv("STATUS_CHANNEL_ID", ""), public_message)
        notified_at = now

    save_state(health, notified_at)
    if health["status"] == "unhealthy":
        logger.error("Health check failed: %s", " | ".join(health["reasons"]))
    return health
