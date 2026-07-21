import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

MAIN_ARCHITECTURE = "arm64"
WORKER_FUNCTION = "ToEnWikipediaBotWorker"
HEALTH_EVENT = {
    "requestContext": {"http": {"method": "GET"}},
    "rawPath": "/health",
}


def error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "Unknown")


def wait_for_update(lambda_client, function_name: str) -> None:
    lambda_client.get_waiter("function_updated_v2").wait(FunctionName=function_name)


def ensure_architecture(lambda_client, function_name: str) -> dict:
    current = lambda_client.get_function_configuration(FunctionName=function_name)
    if (current.get("Architectures") or ["x86_64"]) != [MAIN_ARCHITECTURE]:
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Architectures=[MAIN_ARCHITECTURE],
        )
        wait_for_update(lambda_client, function_name)
        current = lambda_client.get_function_configuration(FunctionName=function_name)
    return current


def update_code(lambda_client, function_name: str, code: bytes) -> None:
    lambda_client.update_function_code(
        FunctionName=function_name,
        ZipFile=code,
        Publish=False,
    )
    wait_for_update(lambda_client, function_name)


def invoke_health(lambda_client, function_name: str) -> dict:
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(HEALTH_EVENT).encode("utf-8"),
    )
    payload = json.loads(response["Payload"].read().decode("utf-8"))
    if response.get("FunctionError"):
        raise RuntimeError(f"{function_name} failed its ARM64 health invocation: {payload}")
    if payload.get("statusCode") != 200:
        raise RuntimeError(f"{function_name} returned an unhealthy response: {payload}")
    body = json.loads(payload.get("body", "{}"))
    if body.get("status") != "ok":
        raise RuntimeError(f"{function_name} returned an invalid health body: {payload}")
    return body


def ensure_worker_canary(lambda_client, main_config: dict, code: bytes) -> None:
    try:
        current = ensure_architecture(lambda_client, WORKER_FUNCTION)
        update_code(lambda_client, WORKER_FUNCTION, code)
        merged_environment = dict(
            current.get("Environment", {}).get("Variables", {})
        )
        merged_environment.update(
            main_config.get("Environment", {}).get("Variables", {})
        )
        lambda_client.update_function_configuration(
            FunctionName=WORKER_FUNCTION,
            Runtime="python3.13",
            Handler="ToEnWikipediaBot.lambda_handler",
            Role=main_config["Role"],
            MemorySize=256,
            Timeout=15,
            Environment={"Variables": merged_environment},
        )
        wait_for_update(lambda_client, WORKER_FUNCTION)
    except ClientError as error:
        if error_code(error) != "ResourceNotFoundException":
            raise
        lambda_client.create_function(
            FunctionName=WORKER_FUNCTION,
            Runtime="python3.13",
            Role=main_config["Role"],
            Handler="ToEnWikipediaBot.lambda_handler",
            Code={"ZipFile": code},
            Description="ARM64 canary and SQS worker for ToEnWikipediaBot",
            Timeout=15,
            MemorySize=256,
            Publish=False,
            Environment={
                "Variables": main_config.get("Environment", {}).get("Variables", {})
            },
            Tags={"Application": "ToEnWikipediaBot"},
            Architectures=[MAIN_ARCHITECTURE],
        )
        lambda_client.get_waiter("function_active_v2").wait(
            FunctionName=WORKER_FUNCTION
        )

    invoke_health(lambda_client, WORKER_FUNCTION)


def main() -> None:
    region = os.environ["AWS_REGION"]
    function_name = os.getenv("LAMBDA_FUNCTION_NAME", "ToEnWikipediaBot")
    package = Path(os.getenv("BOT_ZIP_PATH", "package-arm64.zip"))
    if not package.exists():
        raise FileNotFoundError(package)

    lambda_client = boto3.client("lambda", region_name=region)
    code = package.read_bytes()
    main_config = lambda_client.get_function_configuration(FunctionName=function_name)

    # The worker is the canary: the exact ARM package must import and answer a
    # real Lambda invocation before the public webhook function is migrated.
    ensure_worker_canary(lambda_client, main_config, code)

    ensure_architecture(lambda_client, function_name)
    update_code(lambda_client, function_name, code)
    health = invoke_health(lambda_client, function_name)

    print(
        json.dumps(
            {
                "architecture": MAIN_ARCHITECTURE,
                "worker_canary": "healthy",
                "main_function": function_name,
                "main_health": health,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
