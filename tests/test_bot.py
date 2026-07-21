import base64
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import ToEnWikipediaBot as bot


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, params=None):
        return FakeResponse(self.payload)


class FakeClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class UrlTests(unittest.TestCase):
    def test_normalizes_mobile_and_long_language_code(self):
        link = bot.normalize_wikipedia_url(
            "http://zh-min-nan.m.wikipedia.org/wiki/Albert_Einstein#History"
        )
        self.assertIsNotNone(link)
        self.assertEqual(link.language_code, "zh-min-nan")
        self.assertEqual(
            link.url,
            "https://zh-min-nan.wikipedia.org/wiki/Albert_Einstein",
        )

    def test_rejects_lookalike_and_non_article_domains(self):
        self.assertIsNone(
            bot.normalize_wikipedia_url("https://wikipedia.org.evil.example/wiki/Test")
        )
        self.assertIsNone(
            bot.normalize_wikipedia_url("https://it.wikipedia.org/w/index.php?title=Test")
        )

    def test_deduplicates_and_skips_english(self):
        links = bot.extract_wikipedia_links(
            [
                "https://it.wikipedia.org/wiki/Roma",
                "https://it.wikipedia.org/wiki/Roma#Storia",
                "https://en.wikipedia.org/wiki/Rome",
            ]
        )
        self.assertEqual(
            [link.url for link in links],
            ["https://it.wikipedia.org/wiki/Roma"],
        )

    def test_removes_unmatched_trailing_punctuation(self):
        links = bot.extract_urls_from_text(
            "See (https://it.wikipedia.org/wiki/Roma), please."
        )
        self.assertEqual(links, ["https://it.wikipedia.org/wiki/Roma"])


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    def test_decodes_base64_function_url_body(self):
        payload = {"update_id": 123}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        self.assertEqual(
            bot._decode_event_body({"body": encoded, "isBase64Encoded": True}),
            payload,
        )

    def test_rejects_invalid_json(self):
        with self.assertRaises(ValueError):
            bot._decode_event_body({"body": "not-json"})

    def test_webhook_secret_is_required(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(bot._webhook_is_authorized({"headers": {}}))

    def test_webhook_secret_is_constant_time_checked(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_WEBHOOK_SECRET": "expected"},
            clear=False,
        ):
            self.assertTrue(
                bot._webhook_is_authorized(
                    {
                        "headers": {
                            "x-telegram-bot-api-secret-token": "expected"
                        }
                    }
                )
            )
            self.assertFalse(bot._webhook_is_authorized({"headers": {}}))

    async def test_public_health_endpoint(self):
        response = await bot._handle_http_event(
            {
                "requestContext": {"http": {"method": "GET"}},
                "rawPath": "/health",
            }
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["status"], "ok")

    async def test_invalid_secret_is_rejected_before_processing(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_WEBHOOK_SECRET": "expected"},
            clear=True,
        ):
            response = await bot._handle_http_event(
                {
                    "requestContext": {"http": {"method": "POST"}},
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps({"update_id": 1}),
                }
            )
        self.assertEqual(response["statusCode"], 403)

    async def test_relevant_message_is_queued_before_acknowledgement(self):
        update = {
            "update_id": 1,
            "message": {
                "date": int(time.time()),
                "text": "https://it.wikipedia.org/wiki/Roma",
                "chat": {"id": 1, "type": "private"},
            },
        }
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {
                "content-type": "application/json",
                "x-telegram-bot-api-secret-token": "secret",
            },
            "body": json.dumps(update),
        }
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_WEBHOOK_SECRET": "secret",
                "SQS_QUEUE_URL": "https://queue.example",
            },
            clear=True,
        ), patch.object(bot, "enqueue_update") as enqueue:
            response = await bot._handle_http_event(event)
        self.assertEqual(response["statusCode"], 200)
        enqueue.assert_called_once_with(update)


class ClassificationTests(unittest.TestCase):
    def test_commands_and_inline_queries_are_synchronous(self):
        self.assertEqual(
            bot.classify_update(
                {"message": {"text": "/status", "chat": {"type": "private"}}}
            ),
            "synchronous",
        )
        self.assertEqual(bot.classify_update({"inline_query": {"query": "x"}}), "synchronous")

    def test_irrelevant_group_text_is_not_queued(self):
        self.assertEqual(
            bot.classify_update(
                {"message": {"text": "hello", "chat": {"type": "group"}}}
            ),
            "ignored",
        )

    def test_arbitrary_url_entity_is_not_queued(self):
        update = {
            "message": {
                "text": "https://example.com",
                "entities": [{"type": "url", "offset": 0, "length": 19}],
                "chat": {"type": "group"},
            }
        }
        self.assertEqual(bot.classify_update(update), "ignored")

    def test_wikipedia_text_link_entity_is_queued(self):
        update = {
            "message": {
                "text": "read this",
                "entities": [
                    {
                        "type": "text_link",
                        "offset": 0,
                        "length": 4,
                        "url": "https://it.wikipedia.org/wiki/Roma",
                    }
                ],
                "chat": {"type": "group"},
            }
        }
        self.assertEqual(bot.classify_update(update), "queued")

    def test_stale_group_messages_are_skipped(self):
        policy = bot._stale_policy(
            {
                "message": {
                    "date": 100,
                    "chat": {"id": -1, "type": "supergroup"},
                }
            },
            now=100 + bot.GROUP_STALE_SECONDS + 1,
        )
        self.assertEqual(policy, ("skip-group", False))

    def test_moderately_delayed_private_message_is_processed_with_notice(self):
        policy = bot._stale_policy(
            {
                "message": {
                    "date": 100,
                    "chat": {"id": 1, "type": "private"},
                }
            },
            now=100 + bot.GROUP_STALE_SECONDS + 1,
        )
        self.assertEqual(policy, ("process", True))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_without_effective_message_is_ignored(self):
        update = SimpleNamespace(effective_message=None, effective_sender=None)
        await bot.check_wiki_link(update, None)

    async def test_plain_text_does_not_consume_rate_limit_or_reply(self):
        message = SimpleNamespace(
            text="hello",
            entities=(),
            message_id=1,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_sender=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=1),
        )
        with patch.object(bot, "allow_conversion") as limiter:
            await bot.check_wiki_link(update, None)
        limiter.assert_not_called()
        message.reply_text.assert_not_awaited()

    async def test_successful_conversion_replies(self):
        message = SimpleNamespace(
            text="https://it.wikipedia.org/wiki/Roma",
            entities=(),
            message_id=99,
            reply_text=AsyncMock(),
        )
        application = SimpleNamespace(bot_data={})
        context = SimpleNamespace(application=application)
        update = SimpleNamespace(
            effective_message=message,
            effective_sender=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=1),
        )
        with patch.object(bot, "allow_conversion", return_value=True), patch.object(
            bot,
            "process_link",
            AsyncMock(return_value="converted"),
        ), patch.object(bot.aiohttp, "ClientSession", FakeClientSession):
            await bot.check_wiki_link(update, context)

        message.reply_text.assert_awaited_once_with(
            "converted",
            reply_to_message_id=99,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def test_url_entity_uses_utf16_safe_parser(self):
        entity = SimpleNamespace(type=bot.MessageEntity.URL, url=None)
        message = SimpleNamespace(
            text="😀 https://it.wikipedia.org/wiki/Roma",
            entities=(entity,),
            parse_entity=lambda current: "https://it.wikipedia.org/wiki/Roma",
        )
        urls = bot.extract_urls_from_message(message)
        self.assertIn("https://it.wikipedia.org/wiki/Roma", urls)

    async def test_mediawiki_langlinks_response(self):
        payload = {
            "query": {
                "pages": [
                    {
                        "title": "Roma",
                        "langlinks": [{"lang": "en", "title": "Rome"}],
                    }
                ]
            }
        }
        link = bot.WikipediaLink(
            "https://it.wikipedia.org/wiki/Roma",
            "it",
            "Roma",
        )
        result = await bot.get_english_wikipedia_url(FakeSession(payload), link)
        self.assertIn("https://en.wikipedia.org/wiki/Rome", result)


class CommandRateTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limited_command_is_silently_ignored(self):
        message = SimpleNamespace(message_id=1, reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=1),
        )
        with patch.object(bot, "allow_command", return_value=False):
            await bot.source(update, None)
        message.reply_text.assert_not_awaited()


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_update_is_ignored(self):
        record = {
            "messageId": "m1",
            "body": json.dumps({"update_id": 10}),
        }
        with patch.object(bot, "claim_update", return_value=False), patch.object(
            bot, "_process_update_data", new=AsyncMock()
        ) as process:
            result = await bot._handle_sqs_event(
                {"Records": [{**record, "eventSource": "aws:sqs"}]}
            )
        self.assertEqual(result, {"batchItemFailures": []})
        process.assert_not_awaited()

    async def test_failed_record_is_returned_as_partial_batch_failure(self):
        update = {
            "update_id": 10,
            "message": {
                "date": int(time.time()),
                "text": "https://it.wikipedia.org/wiki/Roma",
                "chat": {"id": 1, "type": "private"},
            },
        }
        with patch.object(bot, "claim_update", return_value=True), patch.object(
            bot, "_process_update_data", new=AsyncMock(side_effect=RuntimeError("boom"))
        ), patch.object(bot, "fail_update"):
            result = await bot._handle_sqs_event(
                {
                    "Records": [
                        {
                            "messageId": "m1",
                            "eventSource": "aws:sqs",
                            "body": json.dumps(update),
                        }
                    ]
                }
            )
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m1"}]})


if __name__ == "__main__":
    unittest.main()
