import os
import unittest
from unittest.mock import patch

import monitor


class MonitorTests(unittest.TestCase):
    def test_notification_bot_does_not_replace_monitored_bot(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "main-token",
                "STATUS_BOT_TOKEN": "status-token",
            },
            clear=True,
        ):
            self.assertEqual(monitor._main_token(), "main-token")
            self.assertEqual(monitor._notification_token(), "status-token")

    def test_healthy_state(self):
        with patch.dict(
            os.environ,
            {"FUNCTION_URL": "https://example.lambda-url.aws/"},
            clear=True,
        ), patch.object(
            monitor,
            "_http_json",
            return_value={
                "status": "ok",
                "version": "abc123",
                "queue_configured": True,
            },
        ), patch.object(
            monitor,
            "_telegram",
            return_value={
                "url": "https://example.lambda-url.aws/",
                "pending_update_count": 0,
            },
        ), patch.object(
            monitor,
            "_queue_attributes",
            side_effect=[
                {"visible": 0, "in_flight": 0, "age": 0},
                {"visible": 0, "in_flight": 0, "age": 0},
            ],
        ):
            health = monitor.collect_health()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["reasons"], [])

    def test_historical_webhook_error_does_not_delay_recovery(self):
        with patch.dict(
            os.environ,
            {"FUNCTION_URL": "https://example.lambda-url.aws/"},
            clear=True,
        ), patch.object(
            monitor,
            "_http_json",
            return_value={
                "status": "ok",
                "version": "abc123",
                "queue_configured": True,
            },
        ), patch.object(
            monitor,
            "_telegram",
            return_value={
                "url": "https://example.lambda-url.aws/",
                "pending_update_count": 0,
                "last_error_date": 2**31,
                "last_error_message": "old transient error",
            },
        ), patch.object(
            monitor,
            "_queue_attributes",
            side_effect=[
                {"visible": 0, "in_flight": 0, "age": 0},
                {"visible": 0, "in_flight": 0, "age": 0},
            ],
        ):
            health = monitor.collect_health()
        self.assertEqual(health["status"], "healthy")

    def test_outage_transition_notifies_once(self):
        health = {
            "status": "unhealthy",
            "checked_at": 1000,
            "pending_updates": 5,
            "queue_depth": 2,
            "queue_age": 180,
            "dlq_depth": 0,
            "endpoint_version": "abc",
            "reasons": ["test outage"],
        }
        with patch.object(monitor, "collect_health", return_value=health), patch.object(
            monitor, "load_state", return_value={"status": "healthy", "notified_at": "0"}
        ), patch.object(monitor, "save_state") as save, patch.object(
            monitor, "_publish_sns"
        ) as publish, patch.object(monitor, "_send_telegram") as send:
            result = monitor.handler({}, None)
        self.assertEqual(result["status"], "unhealthy")
        publish.assert_called_once()
        self.assertGreaterEqual(send.call_count, 1)
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
