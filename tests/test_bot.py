import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    def test_decodes_base64_api_gateway_body(self):
        payload = {"update_id": 123}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        self.assertEqual(
            bot._decode_event_body({"body": encoded, "isBase64Encoded": True}),
            payload,
        )

    def test_rejects_invalid_json(self):
        with self.assertRaises(ValueError):
            bot._decode_event_body({"body": "not-json"})

    def test_optional_webhook_secret(self):
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


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.user_requests.clear()

    async def test_update_without_effective_message_is_ignored(self):
        update = SimpleNamespace(effective_message=None, effective_sender=None)
        await bot.check_wiki_link(update, None)

    async def test_plain_text_does_not_raise_or_reply(self):
        message = SimpleNamespace(
            text="hello",
            entities=(),
            message_id=1,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_sender=SimpleNamespace(id=1),
        )
        await bot.check_wiki_link(update, None)
        message.reply_text.assert_not_awaited()

    async def test_successful_conversion_replies(self):
        message = SimpleNamespace(
            text="https://it.wikipedia.org/wiki/Roma",
            entities=(),
            message_id=99,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_sender=SimpleNamespace(id=1),
        )
        with patch.object(
            bot,
            "process_link",
            AsyncMock(return_value="converted"),
        ), patch.object(bot.aiohttp, "ClientSession", FakeClientSession):
            await bot.check_wiki_link(update, None)

        message.reply_text.assert_awaited_once_with(
            "converted",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_to_message_id=99,
        )

    async def test_url_entity_uses_utf16_safe_parser(self):
        entity = SimpleNamespace(type=bot.MessageEntity.URL, url=None)
        message = SimpleNamespace(
            text="😀 https://it.wikipedia.org/wiki/Roma",
            entities=(entity,),
            message_id=1,
            parse_entity=lambda current: "https://it.wikipedia.org/wiki/Roma",
            reply_text=AsyncMock(),
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


if __name__ == "__main__":
    unittest.main()
