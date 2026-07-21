import json
import os
import time

import boto3
from botocore.exceptions import ClientError

REPOSITORY = "jnton/english-wikipedia-link-converter-telegram-bot"
OWNER_ID = "84038748"
REPOSITORY_ID = "782735600"
ROLE_NAME = "GitHubActions-ToEnWikipediaBot"
OIDC_URL = "https://token.actions.githubusercontent.com"
OIDC_HOST = "token.actions.githubusercontent.com"
# AWS normally validates GitHub through its trusted CA store. Supplying both
# published GitHub intermediate thumbprints also keeps this bootstrap compatible
# with SDK/IAM versions whose request model still requires ThumbprintList.
GITHUB_OIDC_THUMBPRINTS = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
]
IAM_CONSISTENCY_RETRIES = 8


def _error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "Unknown")


def ensure_provider(iam, account_id: str) -> str:
    provider_arn = f"arn:aws:iam::{account_id}:oidc-provider/{OIDC_HOST}"
    try:
        provider = iam.get_open_id_connect_provider(
            OpenIDConnectProviderArn=provider_arn
        )
        client_ids = set(provider.get("ClientIDList", []))
        if "sts.amazonaws.com" not in client_ids:
            iam.add_client_id_to_open_id_connect_provider(
                OpenIDConnectProviderArn=provider_arn,
                ClientID="sts.amazonaws.com",
            )
        return provider_arn
    except ClientError as error:
        if _error_code(error) != "NoSuchEntity":
            raise

    try:
        response = iam.create_open_id_connect_provider(
            Url=OIDC_URL,
            ClientIDList=["sts.amazonaws.com"],
            ThumbprintList=GITHUB_OIDC_THUMBPRINTS,
            Tags=[{"Key": "Application", "Value": "ToEnWikipediaBot"}],
        )
        return response["OpenIDConnectProviderArn"]
    except ClientError as error:
        # Another concurrent deployment may have created it after our lookup.
        if _error_code(error) != "EntityAlreadyExists":
            raise
        return provider_arn


def trust_policy(provider_arn: str) -> dict:
    traditional = f"repo:{REPOSITORY}:ref:refs/heads/main"
    immutable = (
        f"repo:jnton@{OWNER_ID}/english-wikipedia-link-converter-telegram-bot@"
        f"{REPOSITORY_ID}:ref:refs/heads/main"
    )
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
                    },
                    "StringLike": {
                        f"{OIDC_HOST}:sub": [traditional, immutable],
                    },
                },
            }
        ],
    }


def deployment_policy(account_id: str, region: str, execution_role_arn: str) -> dict:
    lambda_arns = [
        f"arn:aws:lambda:{region}:{account_id}:function:ToEnWikipediaBot",
        f"arn:aws:lambda:{region}:{account_id}:function:ToEnWikipediaBotWorker",
        f"arn:aws:lambda:{region}:{account_id}:function:ToEnWikipediaBotMonitor",
    ]
    worker_arn = f"arn:aws:lambda:{region}:{account_id}:function:ToEnWikipediaBotWorker"
    managed_role_arns = [
        execution_role_arn,
        f"arn:aws:iam::{account_id}:role/ToEnWikipediaBotMonitorRole",
        f"arn:aws:iam::{account_id}:role/ToEnWikipediaBotSchedulerRole",
        f"arn:aws:iam::{account_id}:role/{ROLE_NAME}",
    ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "LambdaDeployment",
                "Effect": "Allow",
                "Action": [
                    "lambda:GetFunction",
                    "lambda:GetFunctionConfiguration",
                    "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",
                    "lambda:PutFunctionConcurrency",
                    "lambda:GetFunctionConcurrency",
                    "lambda:CreateFunction",
                    "lambda:TagResource",
                    "lambda:GetFunctionUrlConfig",
                    "lambda:CreateFunctionUrlConfig",
                    "lambda:UpdateFunctionUrlConfig",
                    "lambda:AddPermission",
                    "lambda:RemovePermission",
                    "lambda:GetPolicy",
                    "lambda:InvokeFunction",
                ],
                "Resource": lambda_arns,
            },
            {
                "Sid": "ListBotEventSourceMappings",
                "Effect": "Allow",
                "Action": "lambda:ListEventSourceMappings",
                "Resource": "*",
            },
            {
                "Sid": "CreateBotEventSourceMapping",
                "Effect": "Allow",
                "Action": "lambda:CreateEventSourceMapping",
                "Resource": "*",
                "Condition": {"ArnEquals": {"lambda:FunctionArn": worker_arn}},
            },
            {
                "Sid": "UpdateBotEventSourceMapping",
                "Effect": "Allow",
                "Action": "lambda:UpdateEventSourceMapping",
                "Resource": f"arn:aws:lambda:{region}:{account_id}:event-source-mapping:*",
                "Condition": {"ArnEquals": {"lambda:FunctionArn": worker_arn}},
            },
            {
                "Sid": "QueueManagement",
                "Effect": "Allow",
                "Action": [
                    "sqs:CreateQueue",
                    "sqs:GetQueueUrl",
                    "sqs:GetQueueAttributes",
                    "sqs:SetQueueAttributes",
                    "sqs:ListQueues",
                    "sqs:TagQueue",
                ],
                "Resource": f"arn:aws:sqs:{region}:{account_id}:ToEnWikipediaBot-*",
            },
            {
                "Sid": "CreateQueueRequiresWildcard",
                "Effect": "Allow",
                "Action": "sqs:CreateQueue",
                "Resource": "*",
            },
            {
                "Sid": "DynamoDbManagement",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:CreateTable",
                    "dynamodb:DescribeTable",
                    "dynamodb:DescribeTimeToLive",
                    "dynamodb:UpdateTimeToLive",
                    "dynamodb:UpdateTable",
                    "dynamodb:TagResource",
                ],
                "Resource": f"arn:aws:dynamodb:{region}:{account_id}:table/ToEnWikipediaBotState",
            },
            {
                "Sid": "SnsManagement",
                "Effect": "Allow",
                "Action": [
                    "sns:CreateTopic",
                    "sns:GetTopicAttributes",
                    "sns:SetTopicAttributes",
                    "sns:ListSubscriptionsByTopic",
                    "sns:Subscribe",
                    "sns:TagResource",
                ],
                "Resource": f"arn:aws:sns:{region}:{account_id}:ToEnWikipediaBot-alerts",
            },
            {
                "Sid": "MonitoringConfiguration",
                "Effect": "Allow",
                "Action": [
                    "cloudwatch:PutMetricAlarm",
                    "cloudwatch:DescribeAlarms",
                    "cloudwatch:DeleteAlarms",
                    "cloudwatch:TagResource",
                    "logs:CreateLogGroup",
                    "logs:PutRetentionPolicy",
                    "logs:TagResource",
                ],
                "Resource": "*",
            },
            {
                "Sid": "SchedulerConfiguration",
                "Effect": "Allow",
                "Action": [
                    "scheduler:GetSchedule",
                    "scheduler:CreateSchedule",
                    "scheduler:UpdateSchedule",
                ],
                "Resource": f"arn:aws:scheduler:{region}:{account_id}:schedule/default/ToEnWikipediaBot-health-check",
            },
            {
                "Sid": "ReadGitHubOidcProvider",
                "Effect": "Allow",
                "Action": [
                    "iam:GetOpenIDConnectProvider",
                    "iam:AddClientIDToOpenIDConnectProvider",
                ],
                "Resource": f"arn:aws:iam::{account_id}:oidc-provider/{OIDC_HOST}",
            },
            {
                "Sid": "RoleManagement",
                "Effect": "Allow",
                "Action": [
                    "iam:GetRole",
                    "iam:CreateRole",
                    "iam:UpdateAssumeRolePolicy",
                    "iam:PutRolePolicy",
                    "iam:PassRole",
                    "iam:TagRole",
                ],
                "Resource": managed_role_arns,
            },
            {
                "Sid": "BudgetMonitoring",
                "Effect": "Allow",
                "Action": [
                    "budgets:DescribeBudget",
                    "budgets:DescribeBudgets",
                    "budgets:CreateBudget",
                ],
                "Resource": "*",
            },
        ],
    }


def ensure_deployment_role(iam, provider_arn: str) -> None:
    policy_document = json.dumps(trust_policy(provider_arn))
    for attempt in range(IAM_CONSISTENCY_RETRIES):
        try:
            try:
                iam.get_role(RoleName=ROLE_NAME)
                iam.update_assume_role_policy(
                    RoleName=ROLE_NAME,
                    PolicyDocument=policy_document,
                )
            except ClientError as error:
                if _error_code(error) != "NoSuchEntity":
                    raise
                iam.create_role(
                    RoleName=ROLE_NAME,
                    Description="GitHub Actions deployment role for ToEnWikipediaBot",
                    AssumeRolePolicyDocument=policy_document,
                    Tags=[{"Key": "Application", "Value": "ToEnWikipediaBot"}],
                )
            return
        except ClientError as error:
            # IAM can briefly reject the newly-created OIDC provider as an
            # invalid principal while the provider propagates globally.
            code = _error_code(error)
            message = error.response.get("Error", {}).get("Message", "")
            retryable = code in {
                "InvalidInput",
                "MalformedPolicyDocument",
                "NoSuchEntity",
                "ServiceFailure",
            } or "principal" in message.lower()
            if not retryable or attempt == IAM_CONSISTENCY_RETRIES - 1:
                raise
            time.sleep(min(2 ** attempt, 15))


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
    ensure_deployment_role(iam, provider_arn)
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="ToEnWikipediaBotDeployment",
        PolicyDocument=json.dumps(
            deployment_policy(account_id, region, execution_role_arn)
        ),
    )
    print(f"OIDC deployment role ready: arn:aws:iam::{account_id}:role/{ROLE_NAME}")


if __name__ == "__main__":
    main()
