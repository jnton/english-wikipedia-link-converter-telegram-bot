import json
import os
import time

import boto3
from botocore.exceptions import ClientError

from bootstrap_oidc import (
    GITHUB_OIDC_THUMBPRINTS,
    OIDC_HOST,
    OIDC_URL,
    REPOSITORY,
    ROLE_NAME,
    deployment_policy,
)

RETRIES = 10


def error_details(error: ClientError) -> tuple[str, str]:
    payload = error.response.get("Error", {})
    return payload.get("Code", "Unknown"), payload.get("Message", str(error))


def annotate_failure(stage: str, error: ClientError) -> None:
    code, message = error_details(error)
    safe_message = message.replace("\r", " ").replace("\n", " ")
    print(f"::error title=AWS OIDC {stage} failed::{code}: {safe_message}")


def ensure_provider(iam, account_id: str) -> str:
    provider_arn = f"arn:aws:iam::{account_id}:oidc-provider/{OIDC_HOST}"
    try:
        provider = iam.get_open_id_connect_provider(
            OpenIDConnectProviderArn=provider_arn
        )
    except ClientError as error:
        code, _ = error_details(error)
        if code != "NoSuchEntity":
            annotate_failure("provider lookup", error)
            raise
        try:
            response = iam.create_open_id_connect_provider(
                Url=OIDC_URL,
                ClientIDList=["sts.amazonaws.com"],
                ThumbprintList=GITHUB_OIDC_THUMBPRINTS,
                Tags=[{"Key": "Application", "Value": "ToEnWikipediaBot"}],
            )
            return response["OpenIDConnectProviderArn"]
        except ClientError as create_error:
            create_code, _ = error_details(create_error)
            if create_code != "EntityAlreadyExists":
                annotate_failure("provider creation", create_error)
                raise
            return provider_arn

    if "sts.amazonaws.com" not in set(provider.get("ClientIDList", [])):
        try:
            iam.add_client_id_to_open_id_connect_provider(
                OpenIDConnectProviderArn=provider_arn,
                ClientID="sts.amazonaws.com",
            )
        except ClientError as error:
            annotate_failure("audience repair", error)
            raise
    return provider_arn


def trust_policy(provider_arn: str) -> dict:
    # This repository predates GitHub's July 15, 2026 immutable-subject default,
    # so its issued subject is the traditional exact repository/ref value.
    subject = f"repo:{REPOSITORY}:ref:refs/heads/main"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": provider_arn},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{OIDC_HOST}:aud": "sts.amazonaws.com",
                        f"{OIDC_HOST}:sub": subject,
                    }
                },
            }
        ],
    }


def ensure_role(iam, provider_arn: str) -> None:
    policy_document = json.dumps(trust_policy(provider_arn))
    for attempt in range(RETRIES):
        try:
            try:
                iam.get_role(RoleName=ROLE_NAME)
                iam.update_assume_role_policy(
                    RoleName=ROLE_NAME,
                    PolicyDocument=policy_document,
                )
            except ClientError as error:
                code, _ = error_details(error)
                if code != "NoSuchEntity":
                    raise
                iam.create_role(
                    RoleName=ROLE_NAME,
                    Description="GitHub Actions deployment role for ToEnWikipediaBot",
                    AssumeRolePolicyDocument=policy_document,
                    Tags=[{"Key": "Application", "Value": "ToEnWikipediaBot"}],
                )
            return
        except ClientError as error:
            code, message = error_details(error)
            retryable = code in {
                "InvalidInput",
                "MalformedPolicyDocument",
                "NoSuchEntity",
                "ServiceFailure",
            } or "principal" in message.lower()
            if not retryable or attempt == RETRIES - 1:
                annotate_failure("role creation", error)
                raise
            time.sleep(min(2 ** attempt, 20))


def main() -> None:
    region = os.environ["AWS_REGION"]
    function_name = os.getenv("LAMBDA_FUNCTION_NAME", "ToEnWikipediaBot")
    sts = boto3.client("sts")
    iam = boto3.client("iam")
    lambda_client = boto3.client("lambda", region_name=region)

    account_id = sts.get_caller_identity()["Account"]
    execution_role_arn = lambda_client.get_function_configuration(
        FunctionName=function_name
    )["Role"]
    provider_arn = ensure_provider(iam, account_id)
    ensure_role(iam, provider_arn)

    try:
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName="ToEnWikipediaBotDeployment",
            PolicyDocument=json.dumps(
                deployment_policy(account_id, region, execution_role_arn)
            ),
        )
    except ClientError as error:
        annotate_failure("deployment policy attachment", error)
        raise

    print(f"OIDC deployment role ready: arn:aws:iam::{account_id}:role/{ROLE_NAME}")


if __name__ == "__main__":
    main()
