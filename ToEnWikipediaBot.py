import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

import aiohttp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    MessageEntity,
    Update,
)
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MAX_LINKS_PER_UPDATE = int(os.getenv("MAX_LINKS_PER_UPDATE", "5"))
MAX_WEBHOOK_BODY_BYTES = int(os.getenv("MAX_WEBHOOK_BODY_BYTES", "65536"))
USER_RATE_LIMIT = int(os.getenv("USER_RATE_LIMIT", "10"))
CHAT_RATE_LIMIT = int(os.getenv("CHAT_RATE_LIMIT", "30"))
COMMAND_USER_RATE_LIMIT = int(os.getenv("COMMAND_USER_RATE_LIMIT", "20"))
COMMAND_CHAT_RATE_LIMIT = int(os.getenv("COMMAND_CHAT_RATE_LIMIT", "60"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_WARNING_WINDOW_SECONDS = int(os.getenv("RATE_WARNING_WINDOW_SECONDS", "300"))
PRIVATE_STALE_SECONDS = int(os.getenv("PRIVATE_STALE_SECONDS", "3600"))
GROUP_STALE_SECONDS = int(os.getenv("GROUP_STALE_SECONDS", "900"))
RECOVERY_WINDOW_SECONDS = int(os.getenv("RECOVERY_WINDOW_SECONDS", "21600"))
RECOVERY_PRIVATE_LIMIT = int(os.getenv("RECOVERY_PRIVATE_LIMIT", "3"))
IDEMPOTENCY_TTL_SECONDS = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "172800"))
PROCESSING_LEASE_SECONDS = int(os.getenv("PROCESSING_LEASE_SECONDS", "60"))
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5, connect=2)
USER_AGENT = (
    "EnglishWikipediaLinkConverterBot/2.0 "
    "(https://github.com/jnton/english-wikipedia-link-converter-telegram-bot)"
)
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
LANGUAGE_CODE_PATTERN = re.compile(r"[a-z0-9-]{1,32}")
TRANSIENT_TELEGRAM_ERRORS = (NetworkError, RetryAfter, TimedOut)

_aws_clients: dict[str, Any] = {}


class TemporaryLookupError(RuntimeError):
    """Raised when a Wikipedia lookup should be retried."""


class TemporaryStateError(RuntimeError):
    """Raised when distributed state is temporarily unavailable."""


@dataclass(frozen=True)
class WikipediaLink:
    url: str
    language_code: str
    article_title: str


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _aws_client(service_name: str):
    client = _aws_clients.get(service_name)
    if client is None:
        import boto3

        client = boto3.client(service_name)
        _aws_clients[service_name] = client
    return client


def _clean_url_candidate(url: str) -> str:
    cleaned = url.strip().rstrip(".,;:!?")
    pairs = ((")", "("), ("]", "["), ("}", "{"))
    for closing, opening in pairs:
        while cleaned.endswith(closing) and cleaned.count(closing) > cleaned.count(opening):
            cleaned = cleaned[:-1]
    return cleaned


def normalize_wikipedia_url(url: str) -> WikipediaLink | None:
    candidate = _clean_url_candidate(url)
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None

    host_labels = parsed.hostname.lower().rstrip(".").split(".")
    if len(host_labels) < 3 or host_labels[-2:] != ["wikipedia", "org"]:
        return None

    subdomains = host_labels[:-2]
    if subdomains and subdomains[-1] == "m":
        subdomains = subdomains[:-1]
    if len(subdomains) != 1:
        return None

    language_code = subdomains[0]
    if not LANGUAGE_CODE_PATTERN.fullmatch(language_code):
        return None

    path_prefix = "/wiki/"
    if not parsed.path.startswith(path_prefix):
        return None

    encoded_title = parsed.path[len(path_prefix) :]
    if not encoded_title:
        return None

    decoded_title = unquote(encoded_title).replace("_", " ").strip()
    if not decoded_title:
        return None

    canonical_title = quote(unquote(encoded_title), safe="/:()_,'-~")
    canonical_url = f"https://{language_code}.wikipedia.org/wiki/{canonical_title}"
    return WikipediaLink(canonical_url, language_code, decoded_title)


def extract_wikipedia_links(candidates: Iterable[str]) -> list[WikipediaLink]:
    links: list[WikipediaLink] = []
    seen: set[str] = set()
    for candidate in candidates:
        link = normalize_wikipedia_url(candidate)
        if link is None or link.language_code == "en" or link.url in seen:
            continue
        links.append(link)
        seen.add(link.url)
        if len(links) >= MAX_LINKS_PER_UPDATE:
            break
    return links


def extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return [_clean_url_candidate(match.group(0)) for match in URL_PATTERN.finditer(text)]


def extract_urls_from_message(message) -> list[str]:
    candidates = extract_urls_from_text(getattr(message, "text", None))
    for entity in getattr(message, "entities", ()) or ():
        if entity.type == MessageEntity.URL:
            try:
                candidates.append(message.parse_entity(entity))
            except (RuntimeError, ValueError):
                logger.warning("Could not parse a Telegram URL entity.")
        elif entity.type == MessageEntity.TEXT_LINK and entity.url:
            candidates.append(entity.url)
    return candidates


def is_valid_domain(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return (
        host == "wikipedia.org"
        or host.endswith(".wikipedia.org")
        or host == "wikidata.org"
        or host.endswith(".wikidata.org")
    )


def _telegram_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("YOUR_TELEGRAM_BOT_TOKEN")


def _state_table_name() -> str | None:
    return os.getenv("STATE_TABLE_NAME")


def _ddb_number(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _ddb_string(value: str) -> dict[str, str]:
    return {"S": value}


def _conditional_failed(error: Exception) -> bool:
    response = getattr(error, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def claim_update(update_id: int) -> bool:
    table_name = _state_table_name()
    if not table_name:
        return True
    now = int(time.time())
    try:
        _aws_client("dynamodb").update_item(
            TableName=table_name,
            Key={"pk": _ddb_string(f"update#{update_id}")},
            UpdateExpression=(
                "SET #status = :processing, lease_until = :lease, "
                "expires_at = :expires, attempts = if_not_exists(attempts, :zero) + :one"
            ),
            ConditionExpression=(
                "attribute_not_exists(pk) OR #status = :failed OR lease_until < :now"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":processing": _ddb_string("processing"),
                ":failed": _ddb_string("failed"),
                ":lease": _ddb_number(now + PROCESSING_LEASE_SECONDS),
                ":expires": _ddb_number(now + IDEMPOTENCY_TTL_SECONDS),
                ":now": _ddb_number(now),
                ":zero": _ddb_number(0),
                ":one": _ddb_number(1),
            },
        )
        return True
    except Exception as error:
        if _conditional_failed(error):
            return False
        raise TemporaryStateError("DynamoDB idempotency claim failed") from error


def complete_update(update_id: int) -> None:
    table_name = _state_table_name()
    if not table_name:
        return
    now = int(time.time())
    try:
        _aws_client("dynamodb").update_item(
            TableName=table_name,
            Key={"pk": _ddb_string(f"update#{update_id}")},
            UpdateExpression=(
                "SET #status = :done, completed_at = :now, expires_at = :expires "
                "REMOVE lease_until, last_error"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":done": _ddb_string("done"),
                ":now": _ddb_number(now),
                ":expires": _ddb_number(now + IDEMPOTENCY_TTL_SECONDS),
            },
        )
    except Exception as error:
        raise TemporaryStateError("Could not mark Telegram update complete") from error


def fail_update(update_id: int, error: Exception) -> None:
    table_name = _state_table_name()
    if not table_name:
        return
    now = int(time.time())
    error_name = type(error).__name__[:100]
    try:
        _aws_client("dynamodb").update_item(
            TableName=table_name,
            Key={"pk": _ddb_string(f"update#{update_id}")},
            UpdateExpression=(
                "SET #status = :failed, last_error = :error, "
                "failed_at = :now, expires_at = :expires REMOVE lease_until"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": _ddb_string("failed"),
                ":error": _ddb_string(error_name),
                ":now": _ddb_number(now),
                ":expires": _ddb_number(now + IDEMPOTENCY_TTL_SECONDS),
            },
        )
    except Exception:
        logger.exception("Could not mark Telegram update failed.")


def _increment_counter(key: str, ttl: int) -> int | None:
    table_name = _state_table_name()
    if not table_name:
        return None
    try:
        response = _aws_client("dynamodb").update_item(
            TableName=table_name,
            Key={"pk": _ddb_string(key)},
            UpdateExpression="ADD request_count :one SET expires_at = :expires",
            ExpressionAttributeValues={
                ":one": _ddb_number(1),
                ":expires": _ddb_number(ttl),
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["request_count"]["N"])
    except Exception as error:
        raise TemporaryStateError("DynamoDB counter update failed") from error


def _allow_scoped_action(
    prefix: str,
    user_id: int | None,
    chat_id: int | None,
    user_limit: int,
    chat_limit: int,
) -> bool:
    now = int(time.time())
    window = now // RATE_LIMIT_WINDOW_SECONDS
    ttl = (window + 2) * RATE_LIMIT_WINDOW_SECONDS

    user_count = None
    chat_count = None
    if user_id is not None:
        user_count = _increment_counter(f"rate#{prefix}#user#{user_id}#{window}", ttl)
    if chat_id is not None:
        chat_count = _increment_counter(f"rate#{prefix}#chat#{chat_id}#{window}", ttl)

    return not (
        (user_count is not None and user_count > user_limit)
        or (chat_count is not None and chat_count > chat_limit)
    )


def allow_conversion(user_id: int | None, chat_id: int | None) -> bool:
    return _allow_scoped_action(
        "conversion",
        user_id,
        chat_id,
        USER_RATE_LIMIT,
        CHAT_RATE_LIMIT,
    )


def allow_command(user_id: int | None, chat_id: int | None) -> bool:
    return _allow_scoped_action(
        "command",
        user_id,
        chat_id,
        COMMAND_USER_RATE_LIMIT,
        COMMAND_CHAT_RATE_LIMIT,
    )


def allow_inline(user_id: int | None) -> bool:
    return _allow_scoped_action(
        "inline",
        user_id,
        None,
        COMMAND_USER_RATE_LIMIT,
        COMMAND_CHAT_RATE_LIMIT,
    )


def reserve_notice(chat_id: int, notice_type: str, limit: int = 1) -> tuple[bool, int]:
    now = int(time.time())
    bucket = now // RECOVERY_WINDOW_SECONDS
    ttl = (bucket + 2) * RECOVERY_WINDOW_SECONDS
    count = _increment_counter(f"notice#{notice_type}#{chat_id}#{bucket}", ttl)
    if count is None:
        return True, 1
    return count <= limit, count


def _effective_actor_id(update: Update) -> int | None:
    sender = update.effective_sender
    return getattr(sender, "id", None)


async def get_english_wikipedia_url(
    session: aiohttp.ClientSession,
    link: WikipediaLink,
) -> str | None:
    if link.language_code == "en":
        return None

    wiki_api_url = f"https://{link.language_code}.wikipedia.org/w/api.php"
    if not is_valid_domain(wiki_api_url):
        return None

    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
        "titles": link.article_title,
        "prop": "langlinks",
        "lllang": "en",
        "lllimit": "1",
        "llprop": "url",
    }

    try:
        async with session.get(wiki_api_url, params=params) as response:
            if response.status == 404:
                return None
            if response.status == 429 or response.status >= 500:
                raise TemporaryLookupError(
                    f"MediaWiki returned HTTP {response.status}"
                )
            if response.status != 200:
                logger.warning(
                    "MediaWiki returned HTTP %s for language %s.",
                    response.status,
                    link.language_code,
                )
                return None
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        raise TemporaryLookupError("MediaWiki request failed") from error

    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return None

    page = pages[0]
    correct_title = page.get("title") or link.article_title
    langlinks = page.get("langlinks") or []
    escaped_title = escape(str(correct_title), quote=True)
    escaped_original_url = escape(link.url, quote=True)

    if langlinks:
        english_title = langlinks[0].get("title") or langlinks[0].get("*")
        if english_title:
            english_path = quote(
                str(english_title).replace(" ", "_"),
                safe="/:()_,'-~",
            )
            english_url = f"https://en.wikipedia.org/wiki/{english_path}"
            return (
                f'<b>English Wikipedia page found for '
                f'<a href="{escaped_original_url}">{escaped_title}</a></b>:\n'
                f"{english_url}"
            )

    return (
        f'<b>No English Wikipedia page found for '
        f'<a href="{escaped_original_url}">{escaped_title}</a></b>.'
    )


async def process_link(
    session: aiohttp.ClientSession,
    link: WikipediaLink | str,
) -> str | None:
    if isinstance(link, str):
        parsed_link = normalize_wikipedia_url(link)
        if parsed_link is None:
            return None
        link = parsed_link
    return await get_english_wikipedia_url(session, link)


async def _resolve_links(links: list[WikipediaLink]) -> list[str]:
    if not links:
        return []
    async with aiohttp.ClientSession(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as session:
        results = await asyncio.gather(
            *(process_link(session, link) for link in links),
            return_exceptions=True,
        )

    temporary_errors = [result for result in results if isinstance(result, Exception)]
    if temporary_errors:
        first = temporary_errors[0]
        if isinstance(first, Exception):
            raise first

    return [result for result in results if isinstance(result, str) and result]


async def _reply_message_safely(message, text: str, **kwargs):
    try:
        return await message.reply_text(
            text,
            reply_to_message_id=message.message_id,
            **kwargs,
        )
    except BadRequest as error:
        lowered = str(error).lower()
        if "reply" not in lowered and "message" not in lowered:
            raise
        return await message.reply_text(text, **kwargs)


async def _send_rate_limit_warning(message, chat_id: int | None) -> None:
    if chat_id is not None:
        allowed, _ = reserve_notice(chat_id, "rate-limit", 1)
        if not allowed:
            return
    await _reply_message_safely(
        message,
        "Too many conversion requests. Please wait about a minute and try again.",
    )


async def check_wiki_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    links = extract_wikipedia_links(extract_urls_from_message(message))
    if not links:
        return

    actor_id = _effective_actor_id(update)
    chat_id = getattr(update.effective_chat, "id", None)
    if not allow_conversion(actor_id, chat_id):
        await _send_rate_limit_warning(message, chat_id)
        return

    responses = await _resolve_links(links)
    if not responses:
        return

    delayed = bool(context.application.bot_data.get("delayed_update"))
    prefix = "<i>Delayed while the bot was recovering from an outage.</i>\n\n" if delayed else ""
    await _reply_message_safely(
        message,
        prefix + "\n\n".join(responses),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def inline_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    inline = update.inline_query
    if inline is None:
        return

    user_id = getattr(inline.from_user, "id", None)
    if not allow_inline(user_id):
        await inline.answer([], cache_time=5)
        return

    links = extract_wikipedia_links(extract_urls_from_text(inline.query))
    try:
        responses = await _resolve_links(links)
        aggregated_response = "\n\n".join(responses).strip()
    except TemporaryLookupError:
        aggregated_response = ""
        links = links or [WikipediaLink("", "", "")]

    if aggregated_response:
        if len(links) == 1:
            preview_title = "English Wikipedia Link Found"
            preview_desc = aggregated_response.splitlines()[-1]
        else:
            preview_title = f"{len(responses)} English Wikipedia Links Processed"
            preview_desc = "Click to view converted links"
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=preview_title,
                description=preview_desc,
                input_message_content=InputTextMessageContent(
                    aggregated_response,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                ),
            )
        ]
    elif links:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="Conversion Temporarily Unavailable",
                description="The Wikipedia lookup did not complete. Try again.",
                input_message_content=InputTextMessageContent(
                    "The Wikipedia lookup did not complete. Please try again."
                ),
            )
        ]
    else:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="No Wikipedia Links",
                description="Type or paste a non-English Wikipedia URL.",
                input_message_content=InputTextMessageContent(
                    "Please enter non-English Wikipedia page URL(s)."
                ),
            )
        ]

    await inline.answer(results, cache_time=10)


async def _reply(update: Update, text: str, **kwargs) -> None:
    message = update.effective_message
    if message is None:
        return
    user_id = getattr(update.effective_user, "id", None)
    chat_id = getattr(update.effective_chat, "id", None)
    if not allow_command(user_id, chat_id):
        return
    await _reply_message_safely(message, text, **kwargs)


async def source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        "You can find my source code here:\n"
        "https://github.com/jnton/english-wikipedia-link-converter-telegram-bot/\n\n"
        "Feel free to contribute or fork to create your own version!",
    )


async def license_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    license_text = (
        "<b>License</b>\n\n"
        "The code in this repository is licensed under the "
        '<a href="https://www.gnu.org/licenses/agpl-3.0.en.html">'
        "GNU Affero General Public License v3.0 (AGPL-3.0)</a>, "
        "except where otherwise specified.\n\n"
        "The icon for the <b>English Wikipedia Link Converter</b> Telegram Bot "
        "is licensed under a "
        '<a href="http://creativecommons.org/licenses/by-sa/4.0/">'
        "Creative Commons Attribution-ShareAlike 4.0 International License "
        "(CC BY-SA 4.0)</a>. See the "
        '<a href="https://github.com/jnton/english-wikipedia-link-converter-telegram-bot/tree/main/Telegram-Bot-Icon">'
        "icon directory</a> for more details.\n\n"
        "<b>Image Credits</b>\n\n"
        "The bot's icon incorporates images from the following sources:\n\n"
        "- <b>Wikipedia logo</b>, Version2 by Vanished user 24kwjf10h32h, "
        "Version 1 by Nohat (concept by Paullusmagnus); Wikimedia., is used "
        "under a "
        '<a href="https://creativecommons.org/licenses/by-sa/3.0/">'
        "Creative Commons Attribution-ShareAlike 3.0 Unported License "
        "(CC BY-SA 3.0)</a> and can be found "
        '<a href="https://commons.wikimedia.org/wiki/File:Wikipedia-logo-v2-square.svg">'
        "here on Wikimedia Commons</a>.\n\n"
        "- <b>Left arrow</b>, by Icons8 is licensed under "
        '<a href="https://creativecommons.org/share-your-work/public-domain/cc0/">'
        "CC0</a> and is available "
        '<a href="https://commons.wikimedia.org/wiki/File:Left-arrow_(61413)_-_The_Noun_Project.svg">'
        "here on Wikimedia Commons</a>."
    )
    await _reply(
        update,
        license_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _read_monitor_state() -> dict[str, str]:
    table_name = _state_table_name()
    if not table_name:
        return {}
    try:
        item = _aws_client("dynamodb").get_item(
            TableName=table_name,
            Key={"pk": _ddb_string("monitor#state")},
            ConsistentRead=False,
        ).get("Item", {})
        return {key: next(iter(value.values())) for key, value in item.items()}
    except Exception:
        logger.exception("Could not read monitor state.")
        return {}


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await asyncio.to_thread(_read_monitor_state)
    status = state.get("status", "unknown")
    checked_at = int(state.get("checked_at", "0") or 0)
    pending = state.get("pending_updates", "unknown")
    queue_depth = state.get("queue_depth", "unknown")
    version = os.getenv("DEPLOYMENT_VERSION", "unknown")[:12]
    status_url = os.getenv("STATUS_CHANNEL_URL", "")

    if status == "healthy":
        headline = "✅ Operational"
    elif status == "unhealthy":
        headline = "⚠️ Service disruption detected"
    else:
        headline = "❔ Status not available yet"

    age_text = "unknown"
    if checked_at:
        age_text = f"{max(0, int(time.time()) - checked_at)} seconds ago"

    text = (
        f"<b>Status:</b> {headline}\n"
        f"<b>Last external check:</b> {age_text}\n"
        f"<b>Telegram pending updates:</b> {escape(str(pending))}\n"
        f"<b>Queue depth:</b> {escape(str(queue_depth))}\n"
        f"<b>Version:</b> <code>{escape(version)}</code>"
    )
    if status_url:
        text += f'\n\n<a href="{escape(status_url, quote=True)}">Status updates</a>'
    await _reply(update, text, parse_mode="HTML", disable_web_page_preview=True)


async def send_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    first_name = escape(getattr(user, "first_name", "there"), quote=True)
    bot_username = context.bot.username
    if not bot_username:
        bot_user = await context.bot.get_me()
        bot_username = bot_user.username or "ToEnWikipediaBot"

    status_url = os.getenv("STATUS_CHANNEL_URL", "")
    description_text = (
        f"Hello {first_name}!\n\n"
        "I am the <b>English Wikipedia Link Converter Bot</b>.\n\n"
        "I convert any non-English Wikipedia link into its English equivalent.\n\n"
        "<b>Commands:</b>\n"
        "/help - Display this help message\n"
        "/status - Check the bot's service status\n"
        "/source - Get the link to the bot's source code\n"
        "/license - View the bot's license and image credits\n"
        "/privacy - View the bot's Privacy Policy\n\n"
        "<b>How to Use Me:</b>\n"
        "- Send me any non-English Wikipedia link.\n"
        "- Add me to a group to convert links shared by members.\n"
        f"- Use inline mode by typing <code>@{escape(bot_username, quote=True)}</code> "
        "followed by the links.\n"
    )
    if status_url:
        description_text += (
            f'\nService notices: <a href="{escape(status_url, quote=True)}">status channel</a>.'
        )

    keyboard = [[
        InlineKeyboardButton(
            "Add me to your group",
            url=f"https://t.me/{bot_username}?startgroup=true",
        )
    ]]
    if status_url:
        keyboard.append([InlineKeyboardButton("Service status", url=status_url)])

    await _reply(
        update,
        description_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_info(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_info(update, context)


async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        "Privacy Policy:\n"
        "https://jnton.github.io/english-wikipedia-link-converter-telegram-bot/PRIVACY_POLICY.html",
        disable_web_page_preview=True,
    )


def setup_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("source", source))
    application.add_handler(CommandHandler("license", license_command))
    application.add_handler(CommandHandler("privacy", privacy))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, check_wiki_link)
    )
    application.add_handler(InlineQueryHandler(inline_query))


def _json_response(status_code: int, payload: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(payload),
    }


def _event_header(event: dict, name: str) -> str | None:
    headers = event.get("headers") or {}
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _decode_event_body(event: dict) -> dict:
    if "body" not in event:
        raise ValueError("Missing request body")

    body = event["body"]
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        raise ValueError("Request body must be JSON")
    if len(body.encode("utf-8")) > MAX_WEBHOOK_BODY_BYTES:
        raise OverflowError("Request body is too large")

    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("Invalid base64 request body") from error
        if len(body.encode("utf-8")) > MAX_WEBHOOK_BODY_BYTES:
            raise OverflowError("Request body is too large")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError("Invalid JSON request body") from error
    if not isinstance(parsed, dict):
        raise ValueError("Telegram update must be a JSON object")
    return parsed


def _webhook_is_authorized(event: dict) -> bool:
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if not expected:
        # Backward-compatible migration path: the legacy webhook did not send
        # Telegram's secret header. Provisioning sets this variable before
        # registering the hardened webhook, at which point strict verification
        # activates automatically.
        logger.warning("Webhook secret is not configured; accepting legacy webhook request.")
        return True
    received = _event_header(event, "X-Telegram-Bot-Api-Secret-Token")
    return received is not None and secrets.compare_digest(received, expected)


def _http_method(event: dict) -> str:
    return (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or ""
    ).upper()


def _raw_path(event: dict) -> str:
    return event.get("rawPath") or event.get("path") or "/"


def _is_http_event(event: dict) -> bool:
    return "requestContext" in event or "httpMethod" in event


def _is_sqs_event(event: dict) -> bool:
    records = event.get("Records")
    return bool(records) and all(
        record.get("eventSource") == "aws:sqs" for record in records
    )


def _message_payload(update_data: dict) -> dict | None:
    return update_data.get("message") or update_data.get("channel_post")


def _is_command_update(update_data: dict) -> bool:
    message = _message_payload(update_data)
    text = (message or {}).get("text", "")
    return isinstance(text, str) and text.startswith("/")


def update_might_contain_wikipedia_link(update_data: dict) -> bool:
    """Return True only for structurally valid non-English Wikipedia URLs."""
    message = _message_payload(update_data)
    if not message:
        return False

    candidates = extract_urls_from_text(message.get("text"))
    for entity in message.get("entities") or []:
        if entity.get("type") == "text_link" and entity.get("url"):
            candidates.append(str(entity["url"]))

    return bool(extract_wikipedia_links(candidates))


def classify_update(update_data: dict) -> str:
    if "inline_query" in update_data or _is_command_update(update_data):
        return "synchronous"
    if update_might_contain_wikipedia_link(update_data):
        return "queued"
    return "ignored"


def enqueue_update(update_data: dict) -> None:
    queue_url = os.getenv("SQS_QUEUE_URL")
    if not queue_url:
        raise RuntimeError("SQS_QUEUE_URL is not configured")
    _aws_client("sqs").send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(update_data, separators=(",", ":")),
    )


async def _process_update_data(update_data: dict, delayed: bool = False) -> None:
    token = _telegram_token()
    if not token:
        raise RuntimeError("Telegram bot token is missing")

    application = Application.builder().token(token).build()
    setup_handlers(application)
    application.bot_data["delayed_update"] = delayed
    initialized = False
    try:
        update = Update.de_json(update_data, application.bot)
        if update is None:
            raise ValueError("Invalid Telegram update")
        await application.initialize()
        initialized = True
        await application.process_update(update)
    finally:
        if initialized:
            await application.shutdown()


def _stale_policy(update_data: dict, now: int | None = None) -> tuple[str, bool]:
    message = _message_payload(update_data)
    if not message:
        return "process", False
    message_date = message.get("date")
    if not isinstance(message_date, int):
        return "process", False

    current_time = int(time.time()) if now is None else now
    age = max(0, current_time - message_date)
    chat_type = (message.get("chat") or {}).get("type", "")

    if chat_type == "private":
        if age > PRIVATE_STALE_SECONDS:
            return "skip-private", False
        if age > GROUP_STALE_SECONDS:
            return "process", True
        return "process", False

    if age > GROUP_STALE_SECONDS:
        return "skip-group", False
    return "process", False


async def _telegram_send_message(chat_id: int | str, text: str) -> None:
    token = _telegram_token()
    if not token:
        raise RuntimeError("Telegram bot token is missing")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.post(url, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status == 429 or response.status >= 500:
                raise TemporaryLookupError("Telegram sendMessage temporarily failed")
            if not data.get("ok"):
                description = str(data.get("description", "Telegram sendMessage failed"))
                if response.status in {400, 403}:
                    logger.warning("Recovery notice was not deliverable: %s", description)
                    return
                raise TemporaryLookupError(description)


async def _handle_stale_update(update_data: dict, policy: str) -> None:
    message = _message_payload(update_data) or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return

    if policy == "skip-private":
        allowed, _ = reserve_notice(chat_id, "private-recovery", 1)
        if allowed:
            await _telegram_send_message(
                chat_id,
                "The bot was temporarily unavailable. This older request was skipped "
                "to prevent delayed spam; please resend the Wikipedia link if you still need it.",
            )
    elif policy == "skip-group" and _env_bool("RECOVERY_NOTICES_ENABLED", True):
        allowed, _ = reserve_notice(chat_id, "group-recovery", 1)
        if allowed:
            await _telegram_send_message(
                chat_id,
                "The bot was temporarily unavailable. Older conversions were skipped "
                "to avoid flooding this chat; please resend any link that is still needed.",
            )


async def _process_sqs_record(record: dict) -> None:
    body = record.get("body")
    if not isinstance(body, str):
        raise ValueError("SQS record body is missing")
    update_data = json.loads(body)
    update_id = update_data.get("update_id")
    if not isinstance(update_id, int):
        raise ValueError("Telegram update_id is missing")

    if not claim_update(update_id):
        return

    try:
        policy, delayed = _stale_policy(update_data)
        if policy.startswith("skip"):
            await _handle_stale_update(update_data, policy)
            complete_update(update_id)
            return

        if delayed:
            message = _message_payload(update_data) or {}
            chat_id = (message.get("chat") or {}).get("id")
            if isinstance(chat_id, int):
                allowed, _ = reserve_notice(
                    chat_id,
                    "private-delayed-conversion",
                    RECOVERY_PRIVATE_LIMIT,
                )
                if not allowed:
                    complete_update(update_id)
                    return

        await _process_update_data(update_data, delayed=delayed)
        complete_update(update_id)
    except (Forbidden, BadRequest) as error:
        logger.warning("Telegram update became permanently undeliverable: %s", type(error).__name__)
        complete_update(update_id)
    except Exception as error:
        fail_update(update_id, error)
        raise


async def _handle_sqs_event(event: dict) -> dict:
    failures = []
    for record in event.get("Records", []):
        try:
            await _process_sqs_record(record)
        except Exception:
            logger.exception("SQS record processing failed.")
            failures.append({"itemIdentifier": record.get("messageId", "unknown")})
    return {"batchItemFailures": failures}


async def _handle_http_event(event: dict) -> dict:
    method = _http_method(event)
    path = _raw_path(event)

    if method in {"GET", "HEAD"} and path.rstrip("/") in {"", "/health"}:
        payload = {
            "status": "ok",
            "service": "ToEnWikipediaBot",
            "version": os.getenv("DEPLOYMENT_VERSION", "unknown")[:12],
            "queue_configured": bool(os.getenv("SQS_QUEUE_URL")),
            "timestamp": int(time.time()),
        }
        return _json_response(200, payload)

    if method != "POST":
        return _json_response(405, {"error": "Method not allowed"})

    if not _webhook_is_authorized(event):
        logger.warning("Rejected a webhook request with an invalid or missing secret.")
        return _json_response(403, {"error": "Forbidden"})

    content_type = (_event_header(event, "Content-Type") or "").lower()
    if content_type and "application/json" not in content_type:
        return _json_response(415, {"error": "Content-Type must be application/json"})

    try:
        update_data = _decode_event_body(event)
    except OverflowError as error:
        return _json_response(413, {"error": str(error)})
    except ValueError as error:
        return _json_response(400, {"error": str(error)})

    update_type = classify_update(update_data)
    if update_type == "ignored":
        return _json_response(200, {"message": "Ignored"})

    try:
        if update_type == "queued" and os.getenv("SQS_QUEUE_URL"):
            await asyncio.to_thread(enqueue_update, update_data)
        else:
            await _process_update_data(update_data)
    except TRANSIENT_TELEGRAM_ERRORS:
        logger.exception("Telegram processing failed temporarily.")
        return _json_response(503, {"error": "Temporary Telegram failure"})
    except Exception:
        logger.exception("Webhook processing failed.")
        return _json_response(500, {"error": "Internal server error"})

    return _json_response(200, {"message": "Accepted"})


async def async_lambda_handler(event: dict, context) -> dict:
    if _is_sqs_event(event):
        return await _handle_sqs_event(event)
    if _is_http_event(event):
        return await _handle_http_event(event)
    logger.warning("Ignoring unsupported Lambda event type.")
    return {"ignored": True}


def lambda_handler(event: dict, context) -> dict:
    try:
        return asyncio.run(async_lambda_handler(event, context))
    except Exception:
        logger.exception("Unhandled Lambda entrypoint error.")
        if _is_sqs_event(event):
            raise
        return _json_response(500, {"error": "Internal server error"})
