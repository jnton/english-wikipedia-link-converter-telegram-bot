import os
import secrets

from aws_setup.common import client
from aws_setup.lambda_resources import ensure_function_url, update_main_function
from aws_setup.telegram_setup import configure_webhook


def main() -> None:
    """Restore the known-good synchronous handler with an authenticated webhook."""
    region = os.environ["AWS_REGION"]
    function_name = os.getenv("LAMBDA_FUNCTION_NAME", "ToEnWikipediaBot")
    lambda_client = client("lambda", region)

    current = lambda_client.get_function_configuration(FunctionName=function_name)
    environment = current.get("Environment", {}).get("Variables", {})
    token = environment.get("TELEGRAM_BOT_TOKEN") or environment.get(
        "YOUR_TELEGRAM_BOT_TOKEN"
    )
    if not token:
        raise RuntimeError("The Lambda function has no Telegram bot token environment variable")

    function_url = ensure_function_url(lambda_client, function_name)
    webhook_secret = environment.get("TELEGRAM_WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    update_main_function(
        lambda_client,
        function_name,
        {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_WEBHOOK_SECRET": webhook_secret,
            "FUNCTION_URL": function_url,
        },
    )
    webhook_info = configure_webhook(token, function_url, webhook_secret)
    print(
        "Known-good synchronous bot restored at the authenticated Function URL; "
        f"pending updates: {webhook_info.get('pending_update_count', 0)}"
    )


if __name__ == "__main__":
    main()
