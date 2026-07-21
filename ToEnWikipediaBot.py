import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from html import escape
from typing import Iterable
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

MAX_REQUESTS = 30
WINDOW_SIZE = 60
MAX_TRACKED_USERS = 1000
MAX_LINKS_PER_UPDATE = 10
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=4, connect=2)
USER_AGENT = (
    "EnglishWikipediaLinkConverterBot/1.0 "
    "(https://github.com/jnton/english-wikipedia-link-converter-telegram-bot)"
)
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
LANGUAGE_CODE_PATTERN = re.compile(r"[a-z0-9-]{1,32}")

user_requests: OrderedDict[int, list[float]] = OrderedDict()


@dataclass(frozen=True)
class WikipediaLink:
    url: str
    language_code: str
    article_title: str


def _clean_url_candidate(url: str) -> str:
    """Remove punctuation that is likely outside a pasted URL."""
    cleaned = url.strip().rstrip(".,;:!?")
    pairs = ((")", "("), ("]", "["), ("}", "{"))
    for closing, opening in pairs:
        while cleaned.endswith(closing) and cleaned.count(closing) > cleaned.count(opening):
            cleaned = cleaned[:-1]
    return cleaned


def normalize_wikipedia_url(url: str) -> WikipediaLink | None:
    """Validate and canonicalize a Wikipedia article URL."""
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

    canonical_title = quote(
        unquote(encoded_title),
        safe="/:()_,'-~",
    )
    canonical_url = (
        f"https://{language_code}.wikipedia.org/wiki/{canonical_title}"
    )
    return WikipediaLink(canonical_url, language_code, decoded_title)


def extract_wikipedia_links(candidates: Iterable[str]) -> list[WikipediaLink]:
    """Return unique, non-English Wikipedia article links in input order."""
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
    """Extract plain and entity-backed URLs from a Telegram message."""
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
    """Return whether a URL points to an approved Wikimedia domain."""
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


def check_rate_limit(actor_id: int) -> bool:
    """Apply a best-effort per-container sliding-window rate limit."""
    current_time = time.monotonic()

    if actor_id not in user_requests:
        if len(user_requests) >= MAX_TRACKED_USERS:
            user_requests.popitem(last=False)
        user_requests[actor_id] = []
    else:
        user_requests.move_to_end(actor_id)

    cutoff = current_time - WINDOW_SIZE
    timestamps = [timestamp for timestamp in user_requests[actor_id] if timestamp > cutoff]
    timestamps.append(current_time)
    user_requests[actor_id] = timestamps
    return len(timestamps) <= MAX_REQUESTS


def _effective_actor_id(update: Update) -> int | None:
    sender = update.effective_sender
    return getattr(sender, "id", None)


async def get_english_wikipedia_url(
    session: aiohttp.ClientSession,
    link: WikipediaLink,
) -> str | None:
    """Resolve a Wikipedia page's English interlanguage link with one API call."""
    if link.language_code == "en":
        return None

    wiki_api_url = f"https://{link.language_code}.wikipedia.org/w/api.php"
    if not is_valid_domain(wiki_api_url):
        logger.warning("Blocked invalid MediaWiki API domain.")
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
            if response.status != 200:
                logger.warning(
                    "MediaWiki API returned HTTP %s for language %s.",
                    response.status,
                    link.language_code,
                )
                return None
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        logger.exception("MediaWiki request failed for language %s.", link.language_code)
        return None

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

    responses: list[str] = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(
                "Unexpected link-processing failure: %s",
                type(result).__name__,
            )
        elif result:
            responses.append(result)
    return responses


async def check_wiki_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    actor_id = _effective_actor_id(update)
    if actor_id is not None and not check_rate_limit(actor_id):
        await message.reply_text(
            "You are sending requests too quickly. Please slow down."
        )
        return

    try:
        links = extract_wikipedia_links(extract_urls_from_message(message))
        responses = await _resolve_links(links)
        if not responses:
            return

        await message.reply_text(
            "\n\n".join(responses),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_to_message_id=message.message_id,
        )
    except Exception:
        logger.exception("An error occurred in check_wiki_link.")


async def inline_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    inline = update.inline_query
    if inline is None:
        return

    links = extract_wikipedia_links(extract_urls_from_text(inline.query))
    responses = await _resolve_links(links)
    aggregated_response = "\n\n".join(responses).strip()

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


async def _reply(
    update: Update,
    text: str,
    **kwargs,
) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        text,
        reply_to_message_id=message.message_id,
        **kwargs,
    )


async def source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        "You can find my source code here:\n"
        "https://github.com/jnton/english-wikipedia-link-converter-telegram-bot/\n\n"
        "Feel free to contribute or fork to create your own version!",
    )


async def license(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def send_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    first_name = escape(getattr(user, "first_name", "there"), quote=True)
    bot_username = context.bot.username
    if not bot_username:
        bot_user = await context.bot.get_me()
        bot_username = bot_user.username or "EnglishWikipediaLinkConverterBot"

    description_text = (
        f"Hello {first_name}!\n\n"
        "I am the <b>English Wikipedia Link Converter Bot</b>.\n\n"
        "I convert any non-English Wikipedia link into its English equivalent.\n\n"
        "<b>Commands:</b>\n"
        "/help - Display this help message\n"
        "/source - Get the link to the bot's source code\n"
        "/license - View the bot's license and image credits\n"
        "/privacy - View the bot's Privacy Policy\n\n"
        "<b>How to Use Me:</b>\n"
        "- Send me any non-English Wikipedia link.\n"
        "- Add me to a group to convert links shared by members.\n"
        f"- Use inline mode by typing <code>@{escape(bot_username, quote=True)}</code> "
        "followed by the links.\n"
    )

    keyboard = [[
        InlineKeyboardButton(
            "Add me to your group",
            url=f"https://t.me/{bot_username}?startgroup=true",
        )
    ]]
    await _reply(
        update,
        description_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, check_wiki_link)
    )
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_handler(CommandHandler("source", source))
    application.add_handler(CommandHandler("license", license))
    application.add_handler(CommandHandler("privacy", privacy))


def _json_response(status_code: int, payload: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
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

    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("Invalid base64 request body") from error

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
        return True
    received = _event_header(event, "X-Telegram-Bot-Api-Secret-Token")
    return received is not None and secrets.compare_digest(received, expected)


async def async_lambda_handler(event: dict, context) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("YOUR_TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Telegram bot token environment variable is missing.")
        return _json_response(500, {"error": "Bot configuration error"})

    if not _webhook_is_authorized(event):
        logger.warning("Rejected a request with an invalid webhook secret.")
        return _json_response(403, {"error": "Forbidden"})

    try:
        update_data = _decode_event_body(event)
    except ValueError as error:
        logger.warning("Rejected malformed webhook request: %s", error)
        return _json_response(400, {"error": str(error)})

    application = Application.builder().token(token).build()
    setup_handlers(application)
    initialized = False

    try:
        update = Update.de_json(update_data, application.bot)
        if update is None:
            return _json_response(400, {"error": "Invalid Telegram update"})
        await application.initialize()
        initialized = True
        await application.process_update(update)
        return _json_response(200, {"message": "Success"})
    except Exception:
        logger.exception("An error occurred while processing the Telegram update.")
        return _json_response(500, {"error": "Internal server error"})
    finally:
        if initialized:
            try:
                await application.shutdown()
            except Exception:
                logger.exception("Telegram application shutdown failed.")


def lambda_handler(event: dict, context) -> dict:
    try:
        return asyncio.run(async_lambda_handler(event, context))
    except Exception:
        logger.exception("Unhandled Lambda entrypoint error.")
        return _json_response(500, {"error": "Internal server error"})
