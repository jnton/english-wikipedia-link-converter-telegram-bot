import json
import time
from pathlib import Path

from botocore.exceptions import ClientError

from aws_setup.common import APPLICATION, MONITOR_FUNCTION


def ensure_function_url(lambda_client, function_name: str) -> str:
    try:
        response = lambda_client.get_function_url_config(FunctionName=function_name)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        response = lambda_client.create_function_url_config(
            FunctionName=function_name,
            AuthType="NONE",
            InvokeMode="BUFFERED",
        )
    else:
        if response.get("AuthType") != "NONE" or response.get("InvokeMode") != "BUFFERED":
            response = lambda_client.update_function_url_config(
                FunctionName=function_name,
                AuthType="NONE",
                InvokeMode="BUFFERED",
            )

    permissions = [
        {
            "StatementId": "FunctionURLAllowPublicAccess",
            "Action": "lambda:InvokeFunctionUrl",
            "FunctionUrlAuthType": "NONE",
        },
        {
            "StatementId": "FunctionURLAllowPublicInvoke",
            "Action": "lambda:InvokeFunction",
            "InvokedViaFunctionUrl": True,
        },
    ]
    for permission in permissions:
        try:
            lambda_client.add_permission(
                FunctionName=function_name,
                Principal="*",
                **permission,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceConflictException":
                raise
    return response["FunctionUrl"]


def disable_legacy_api_gateway_invocation(lambda_client, function_name: str) -> None:
    """Remove old API Gateway invoke permissions after Telegram uses the Function URL."""
    try:
        policy_text = lambda_client.get_policy(FunctionName=function_name)["Policy"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            return
        raise

    policy = json.loads(policy_text)
    for statement in policy.get("Statement", []):
        principal = statement.get("Principal", {})
        service = principal.get("Service") if isinstance(principal, dict) else None
        if service != "apigateway.amazonaws.com":
            continue
        statement_id = statement.get("Sid")
        if not statement_id:
            continue
        try:
            lambda_client.remove_permission(
                FunctionName=function_name,
                StatementId=statement_id,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise


def update_main_function(
    lambda_client,
    function_name: str,
    environment: dict[str, str],
) -> dict:
    current = lambda_client.get_function_configuration(FunctionName=function_name)
    merged = dict(current.get("Environment", {}).get("Variables", {}))
    merged.update({key: value for key, value in environment.items() if value != ""})
    lambda_client.update_function_configuration(
        FunctionName=function_name,
        Runtime="python3.13",
        Handler="ToEnWikipediaBot.lambda_handler",
        MemorySize=256,
        Timeout=15,
        Environment={"Variables": merged},
    )
    lambda_client.get_waiter("function_updated_v2").wait(FunctionName=function_name)
    lambda_client.put_function_concurrency(
        FunctionName=function_name,
        ReservedConcurrentExecutions=4,
    )
    return lambda_client.get_function_configuration(FunctionName=function_name)


def ensure_monitor_function(
    lambda_client,
    monitor_zip: Path,
    role_arn: str,
    environment: dict[str, str],
) -> str:
    code = monitor_zip.read_bytes()
    try:
        current = lambda_client.get_function_configuration(FunctionName=MONITOR_FUNCTION)
        lambda_client.update_function_code(
            FunctionName=MONITOR_FUNCTION,
            ZipFile=code,
            Publish=True,
        )
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=MONITOR_FUNCTION)
        merged = dict(current.get("Environment", {}).get("Variables", {}))
        merged.update({key: value for key, value in environment.items() if value != ""})
        lambda_client.update_function_configuration(
            FunctionName=MONITOR_FUNCTION,
            Runtime="python3.13",
            Handler="monitor.handler",
            Role=role_arn,
            MemorySize=128,
            Timeout=15,
            Environment={"Variables": merged},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        for attempt in range(6):
            try:
                lambda_client.create_function(
                    FunctionName=MONITOR_FUNCTION,
                    Runtime="python3.13",
                    Role=role_arn,
                    Handler="monitor.handler",
                    Code={"ZipFile": code},
                    Description="Independent free-tier health monitor for ToEnWikipediaBot",
                    Timeout=15,
                    MemorySize=128,
                    Publish=True,
                    Environment={"Variables": environment},
                    Tags={"Application": APPLICATION},
                    Architectures=["x86_64"],
                )
                break
            except ClientError as create_error:
                if (
                    create_error.response["Error"]["Code"] != "InvalidParameterValueException"
                    or attempt == 5
                ):
                    raise
                time.sleep(5)

    lambda_client.get_waiter("function_updated_v2").wait(FunctionName=MONITOR_FUNCTION)
    lambda_client.put_function_concurrency(
        FunctionName=MONITOR_FUNCTION,
        ReservedConcurrentExecutions=1,
    )
    return lambda_client.get_function_configuration(FunctionName=MONITOR_FUNCTION)[
        "FunctionArn"
    ]


def ensure_event_source(lambda_client, function_name: str, queue_arn: str) -> None:
    mappings = lambda_client.list_event_source_mappings(
        FunctionName=function_name,
        EventSourceArn=queue_arn,
    ).get("EventSourceMappings", [])
    common = {
        "FunctionName": function_name,
        "BatchSize": 1,
        "MaximumBatchingWindowInSeconds": 0,
        "FunctionResponseTypes": ["ReportBatchItemFailures"],
        "ScalingConfig": {"MaximumConcurrency": 2},
        "Enabled": True,
    }
    if mappings:
        lambda_client.update_event_source_mapping(UUID=mappings[0]["UUID"], **common)
    else:
        lambda_client.create_event_source_mapping(EventSourceArn=queue_arn, **common)
