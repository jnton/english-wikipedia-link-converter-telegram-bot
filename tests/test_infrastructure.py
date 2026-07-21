import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bootstrap_oidc
import configure_aws
import restore_service
import verify_lambda_package
from aws_setup import lambda_resources, monitoring, storage, telegram_setup


class DeploymentPolicyTests(unittest.TestCase):
    def test_policy_includes_worker_and_required_management_actions(self):
        policy = bootstrap_oidc.deployment_policy(
            "123456789012",
            "eu-north-1",
            "arn:aws:iam::123456789012:role/existing-runtime-role",
        )
        statements = {statement["Sid"]: statement for statement in policy["Statement"]}
        lambda_policy_resources = statements["LambdaDeployment"]["Resource"]
        self.assertTrue(
            any(
                resource.endswith(":function:ToEnWikipediaBotWorker")
                for resource in lambda_policy_resources
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


class ArchitectureTests(unittest.TestCase):
    def test_existing_lambda_is_migrated_to_arm64(self):
        lambda_client = Mock()
        lambda_client.get_function_configuration.side_effect = [
            {"Architectures": ["x86_64"]},
            {"Architectures": ["arm64"]},
        ]
        waiter = Mock()
        lambda_client.get_waiter.return_value = waiter

        result = lambda_resources._ensure_function_architecture(
            lambda_client,
            "ToEnWikipediaBot",
        )

        lambda_client.update_function_configuration.assert_called_once_with(
            FunctionName="ToEnWikipediaBot",
            Architectures=["arm64"],
        )
        waiter.wait.assert_called_once_with(FunctionName="ToEnWikipediaBot")
        self.assertEqual(result["Architectures"], ["arm64"])

    def test_arm64_elf_package_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            shared_object = Path(directory) / "extension.so"
            header = bytearray(20)
            header[:4] = b"\x7fELF"
            header[5] = 1
            struct.pack_into("<H", header, 18, 183)
            shared_object.write_bytes(header)

            files = verify_lambda_package.verify_package(Path(directory), "arm64")

        self.assertEqual([path.name for path in files], ["extension.so"])

    def test_x86_elf_is_rejected_from_arm64_package(self):
        with tempfile.TemporaryDirectory() as directory:
            shared_object = Path(directory) / "extension.so"
            header = bytearray(20)
            header[:4] = b"\x7fELF"
            header[5] = 1
            struct.pack_into("<H", header, 18, 62)
            shared_object.write_bytes(header)

            with self.assertRaisesRegex(RuntimeError, "expected arm64"):
                verify_lambda_package.verify_package(Path(directory), "arm64")


class DeploymentDiagnosticsTests(unittest.TestCase):
    def test_failed_stage_writes_sanitized_aws_diagnostic(self):
        error = ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "not authorized",
                },
                "ResponseMetadata": {"RequestId": "request-123"},
            },
            "CreateQueue",
        )

        with tempfile.TemporaryDirectory() as directory:
            diagnostic_path = Path(directory) / "deployment-failure.json"
            with patch.object(configure_aws, "DIAGNOSTIC_PATH", diagnostic_path):
                with self.assertRaises(ClientError):
                    configure_aws.run_stage(
                        "Create or update SQS queues",
                        Mock(side_effect=error),
                    )

            payload = json.loads(diagnostic_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["stage"], "Create or update SQS queues")
        self.assertEqual(payload["exception_type"], "ClientError")
        self.assertEqual(payload["operation_name"], "CreateQueue")
        self.assertEqual(payload["aws_error_code"], "AccessDeniedException")
        self.assertEqual(payload["aws_error_message"], "not authorized")
        self.assertEqual(payload["request_id"], "request-123")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", json.dumps(payload))


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


class ServiceRestorationTests(unittest.TestCase):
    def test_restore_updates_only_environment_and_registers_secret_webhook(self):
        lambda_client = Mock()
        lambda_client.get_function_configuration.return_value = {
            "Environment": {
                "Variables": {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "EXISTING_VALUE": "preserved",
                }
            }
        }
        lambda_client.get_function_url_config.return_value = {
            "FunctionUrl": "https://example.lambda-url.aws/"
        }
        waiter = Mock()
        lambda_client.get_waiter.return_value = waiter

        with patch.dict(
            os.environ,
            {
                "AWS_REGION": "eu-north-1",
                "LAMBDA_FUNCTION_NAME": "ToEnWikipediaBot",
            },
            clear=False,
        ), patch.object(
            restore_service,
            "client",
            return_value=lambda_client,
        ), patch.object(
            restore_service,
            "configure_webhook",
            return_value={"pending_update_count": 0},
        ) as configure_webhook, patch.object(
            restore_service.secrets,
            "token_urlsafe",
            return_value="generated-secret",
        ):
            restore_service.main()

        update = lambda_client.update_function_configuration.call_args.kwargs
        self.assertEqual(update["FunctionName"], "ToEnWikipediaBot")
        variables = update["Environment"]["Variables"]
        self.assertEqual(variables["EXISTING_VALUE"], "preserved")
        self.assertEqual(variables["TELEGRAM_WEBHOOK_SECRET"], "generated-secret")
        self.assertEqual(
            variables["FUNCTION_URL"],
            "https://example.lambda-url.aws/",
        )
        self.assertNotIn("Runtime", update)
        self.assertNotIn("MemorySize", update)
        self.assertNotIn("Timeout", update)
        waiter.wait.assert_called_once_with(FunctionName="ToEnWikipediaBot")
        configure_webhook.assert_called_once_with(
            "token",
            "https://example.lambda-url.aws/",
            "generated-secret",
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
