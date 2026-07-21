import os
import secrets
from typing import Callable, TypeVar

from aws_setup.common import client
from aws_setup.telegram_setup import configure_webhook

T = TypeVar("T")


def run_stage(name: str, operation: Callable[..., T], *args, **kwargs) -> T:
    print(f"::group::{name}")
    try:
        result = operation(*args, **kwargs)
        print(f"Completed: {name}")
        return result
    except Exception as error:
        message = str(error).replace("\r", " ").replace("\n", " ")
        print(
            f"::error title=Service restoration failed at {name}::"
            f"{type(error).__name__}: {message}"
        )
        raise
    finally:
        print("::endgroup::")


def main() -> None:
    """Restore the known-good synchronous handler with an authenticated webhook."""
    region = os.environ["AWS_REGION"]
    function_name = os.getenv("LAMBDA_FUNCTION_NAME", "ToEnWikipediaBot")
    lambda_client = client("lambda", region)

    current = run_stage(
        "Read current Lambda configuration",
        lambda_client.get_function_configuration,
        FunctionName=function_name,
    )
    environment = dict(current.get("Environment", {}).get("Variables", {}))
    token = environment.get("TELEGRAM_BOT_TOKEN") or environment.get(
        "YOUR_TELEGRAM_BOT_TOKEN"
    )
    if not token:
        raise RuntimeError("The Lambda function has no Telegram bot token environment variable")

    def read_function_url() -> str:
        try:
            return lambda_client.get_function_url_config(FunctionName=function_name)[
                "FunctionUrl"
            ]
        except Exception:
            existing = environment.get("FUNCTION_URL")
            if existing:
                return existing
            raise

    function_url = run_stage("Read existing Lambda Function URL", read_function_url)
    webhook_secret = environment.get("TELEGRAM_WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    environment.update(
        {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_WEBHOOK_SECRET": webhook_secret,
            "FUNCTION_URL": function_url,
        }
    )

    run_stage(
        "Set only the webhook restoration environment",
        lambda_client.update_function_configuration,
        FunctionName=function_name,
        Environment={"Variables": environment},
    )
    run_stage(
        "Wait for Lambda restoration configuration",
        lambda_client.get_waiter("function_updated_v2").wait,
        FunctionName=function_name,
    )
    webhook_info = run_stage(
        "Register authenticated Telegram webhook",
        configure_webhook,
        token,
        function_url,
        webhook_secret,
    )
    print(
        "Known-good synchronous bot restored at the authenticated Function URL; "
        f"pending updates: {webhook_info.get('pending_update_count', 0)}"
    )


if __name__ == "__main__":
    main()
