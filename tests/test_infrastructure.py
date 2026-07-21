import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bootstrap_oidc
from aws_setup import monitoring, storage, telegram_setup


class DeploymentPolicyTests(unittest.TestCase):
    def test_policy_includes_worker_and_required_management_actions(self):
        policy = bootstrap_oidc.deployment_policy(
            "123456789012",
            "eu-north-1",
            "arn:aws:iam::123456789012:role/existing-runtime-role",
        )
        statements = {statement["Sid"]: statement for statement in policy["Statement"]}
        lambda_resources = statements["LambdaDeployment"]["Resource"]
        self.assertTrue(
            any(
                resource.endswith(":function:ToEnWikipediaBotWorker")
                for resource in lambda_resources
            )
        )
        self.assertIn(
            "dynamodb:UpdateTable",
            statements["DynamoDbManagement"]["Action"],
        )
        self.assertIn(
            "cloudwatch:DescribeAlarms",
            statements["MonitoringConfiguration"]["Action"],
        )
        self.assertIn(
            "budgets:DescribeBudgets",
            statements["BudgetMonitoring"]["Action"],
        )


class TelegramProvisioningTests(unittest.TestCase):
    def test_webhook_is_secret_bounded_and_preserves_pending_updates(self):
        calls = []

        def fake_api(token, method, payload=None):
            calls.append((method, payload))
            if method == "getWebhookInfo":
                return {"url": "https://example.lambda-url.aws/"}
            return True

        with patch.object(telegram_setup, "telegram_api", side_effect=fake_api):
            telegram_setup.configure_webhook(
                "token",
                "https://example.lambda-url.aws/",
                "secret",
            )

        webhook = next(payload for method, payload in calls if method == "setWebhook")
        self.assertEqual(webhook["secret_token"], "secret")
        self.assertEqual(webhook["max_connections"], 3)
        self.assertFalse(webhook["drop_pending_updates"])
        self.assertEqual(
            webhook["allowed_updates"],
            ["message", "channel_post", "inline_query"],
        )


class StorageProvisioningTests(unittest.TestCase):
    def test_new_table_uses_free_tier_capacity_and_ttl(self):
        dynamodb = Mock()
        error = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
            "DescribeTable",
        )
        dynamodb.describe_table.side_effect = [
            error,
            {
                "Table": {
                    "ProvisionedThroughput": {
                        "ReadCapacityUnits": 10,
                        "WriteCapacityUnits": 10,
                    }
                }
            },
        ]
        dynamodb.get_waiter.return_value = Mock()
        dynamodb.describe_time_to_live.return_value = {
            "TimeToLiveDescription": {"TimeToLiveStatus": "DISABLED"}
        }

        storage.ensure_table(dynamodb, "eu-north-1", "123")

        kwargs = dynamodb.create_table.call_args.kwargs
        self.assertEqual(
            kwargs["ProvisionedThroughput"],
            {"ReadCapacityUnits": 10, "WriteCapacityUnits": 10},
        )
        dynamodb.update_time_to_live.assert_called_once()


class MonitoringProvisioningTests(unittest.TestCase):
    def test_managed_alarm_count_respects_free_allowance_cap(self):
        cloudwatch = Mock()
        paginator = Mock()
        paginator.paginate.return_value = [
            {
                "MetricAlarms": [
                    {"AlarmName": "unrelated-1"},
                    {"AlarmName": "unrelated-2"},
                    {"AlarmName": "unrelated-3"},
                    {"AlarmName": "unrelated-4"},
                ]
            }
        ]
        cloudwatch.get_paginator.return_value = paginator

        with patch.object(monitoring, "put_alarm") as put_alarm:
            monitoring.ensure_alarms(cloudwatch, "ToEnWikipediaBot", "arn:sns")

        self.assertEqual(put_alarm.call_count, 6)


if __name__ == "__main__":
    unittest.main()
