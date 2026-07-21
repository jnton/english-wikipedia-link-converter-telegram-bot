import json
import os

import boto3
from botocore.exceptions import ClientError

REPOSITORY = "jnton/english-wikipedia-link-converter-telegram-bot"
OWNER_ID = "84038748"
REPOSITORY_ID = "782735600"
ROLE_NAME = "GitHubActions-ToEnWikipediaBot"
OIDC_URL = "https://token.actions.githubusercontent.com"
OIDC_HOST = "token.actions.githubusercontent.com"


def ensure_provider(iam, account_id: str) -> str:
    provider_arn = f"arn:aws:iam::{account_id}:oidc-provider/{OIDC_HOST}"
    try:
        iam.get_open_id_connect_provider(OpenIDConnectProviderArn=provider_arn)
        return provider_arn
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            raise

    response = iam.create_open_id_connect_provider(
        Url=OIDC_URL,
        ClientIDList=["sts.amazonaws.com"],
        Tags=[{"Key": "Application", "Value": "ToEnWikipediaBot"}],
    )
    return response["OpenIDConnectProviderArn"]


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
                "Condition": {
                    "ArnEquals": {
                        "lambda:FunctionArn": worker_arn,
                    }
                },
            },
            {
                "Sid": "UpdateBotEventSourceMapping",
                "Effect": "Allow",
                "Action": "lambda:UpdateEventSourceMapping",
                "Resource": f"arn:aws:lambda:{region}:{account_id}:event-source-mapping:*",
                "Condition": {
                    "ArnEquals": {
                        "lambda:FunctionArn": worker_arn,
                    }
                },
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
                "Action": "iam:GetOpenIDConnectProvider",
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
    assume_role_policy = trust_policy(provider_arn)
    try:
        iam.get_role(RoleName=ROLE_NAME)
        iam.update_assume_role_policy(
            RoleName=ROLE_NAME,
            PolicyDocument=json.dumps(assume_role_policy),
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_role(
            RoleName=ROLE_NAME,
            Description="GitHub Actions deployment role for ToEnWikipediaBot",
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Tags=[{"Key": "Application", "Value": "ToEnWikipediaBot"}],
        )

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
