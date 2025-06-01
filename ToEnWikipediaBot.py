from telegram.helpers import escape
from urllib.parse import unquote, urlparse
import os
import logging
import re
import aiohttp
import json
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, MessageEntity
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes, InlineQueryHandler
from uuid import uuid4
import traceback
from collections import OrderedDict
import time

# Set logging level to WARNING for production
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Global rate limiting config
MAX_REQUESTS = 30        # max requests per window
WINDOW_SIZE = 60         # window size in seconds
MAX_TRACKED_USERS = 1000 # global cap on distinct users

# Replace simple dict with OrderedDict for LRU eviction
user_requests = OrderedDict()

def is_valid_domain(url: str) -> bool:
    """
    Return True if url’s host ends with wikipedia.org or wikidata.org.
    """
    host = urlparse(url).netloc.lower()
    return host.endswith('.wikipedia.org') or host.endswith('.wikidata.org')

async def check_rate_limit(user_id):
    """
    Enforce a sliding-window rate limit per user and a global cap on tracked users.
    Returns True if under limit, False if exceeded.
    """
    current_time = time.time()

    # If new user and we're at capacity, evict the oldest entry
    if user_id not in user_requests:
        if len(user_requests) >= MAX_TRACKED_USERS:
            user_requests.popitem(last=False)  # remove oldest
        user_requests[user_id] = []
    else:
        # Mark this user as recently used
        user_requests.move_to_end(user_id)

    timestamps = user_requests[user_id]
    # Remove timestamps outside the WINDOW_SIZE
    cutoff = current_time - WINDOW_SIZE
    timestamps = [t for t in timestamps if t > cutoff]

    # Record this request
    timestamps.append(current_time)
    user_requests[user_id] = timestamps

    # Allow only up to MAX_REQUESTS in the window
    return len(timestamps) <= MAX_REQUESTS

async def check_wiki_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await check_rate_limit(user_id):
        await update.message.reply_text("You are sending requests too quickly. Please slow down.")
        return
    try:
        message = update.message     
        links = []
        ordered_unique_links = []  # Initialize as list to maintain order
        seen_titles = set()  # To track unique titles and avoid processing duplicates

        # Check for URLs in plain text
        if message.text:
            links.extend(re.findall(r'https?://[^\s]+', message.text))  # Regex to capture all URLs in the message

        # Check for URLs in entities
        if message.entities:
            for entity in message.entities:
                if entity.type == MessageEntity.URL:
                    url = message.text[entity.offset:entity.offset + entity.length]
                    links.append(url)
                elif entity.type == MessageEntity.TEXT_LINK:
                    links.append(entity.url)

        # Filter for unique non-English Wikipedia links
        for link in links:
            if 'wikipedia.org/wiki/' in link and not link.startswith('https://en.wikipedia.org/wiki/'):
                # Decode only for checking uniqueness and title extraction, keep original URL for processing
                decoded_link = unquote(link)
                title = decoded_link.split('/wiki/')[-1]
                if title not in seen_titles:  # Ensure unique Wikipedia titles
                    ordered_unique_links.append(link)  # Keep the original, encoded link for processing
                    seen_titles.add(title)  # Remember title to ensure uniqueness

        if ordered_unique_links:  # Check if there are any links to process
            responses = []
            async with aiohttp.ClientSession() as session:
                for link in ordered_unique_links:  # Iterate over links maintaining their original order
                    match = re.search(r'https?://([a-z]{2,3})?\.?m?\.?wikipedia\.org/wiki/(.+)', link)
                    if match:
                        language_code, article_title_encoded = match.groups()
                        article_title = unquote(article_title_encoded)  # Decode for API calls/display
                        original_url = f"https://{language_code}.wikipedia.org/wiki/{article_title_encoded}"  # Use encoded title for URL
                        if language_code != 'en':  # Process only if not already in English
                            response = await get_english_wikipedia_url(session, original_url, article_title, language_code)
                            if response:
                                responses.append(response)  # Collect and append response
                
        if responses:  # If there are responses to send back
            reply_message = "\n\n".join(responses)  # Aggregate responses into a single message
            await update.message.reply_text(
                reply_message,
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_to_message_id=update.message.message_id  # Explicitly set the reply
            )  # Send reply

    except Exception:
        # ERROR level includes traceback without using DEBUG
        logger.exception("An error occurred in check_wiki_link.")
        return

async def get_english_wikipedia_url(session, original_url, article_title, language_code):
    wiki_api_url = f"https://{language_code}.wikipedia.org/w/api.php"

    if not is_valid_domain(wiki_api_url):
        logger.warning("Attempted to access an invalid domain.")
        return None

    if language_code == 'en':
        return None

    params = {
        'action': 'query',
        'format': 'json',
        'titles': article_title.replace('_', ' '),
        'prop': 'pageprops|info',
        'inprop': 'url'
    }
    async with session.get(wiki_api_url, params=params) as response:
        if response.status == 200:
            data = await response.json()
            pages = next(iter(data['query']['pages'].values()))
            correct_title = pages.get('title', article_title.replace('_', ' '))
            wikidata_id = pages.get('pageprops', {}).get('wikibase_item')
            if wikidata_id:
                wikidata_url = f"https://www.wikidata.org/wiki/Special:EntityData/{wikidata_id}.json"
                async with session.get(wikidata_url) as wd_response:
                    if wd_response.status == 200:
                        wd_data = await wd_response.json()
                        if 'enwiki' in wd_data['entities'][wikidata_id]['sitelinks']:
                            en_title = wd_data['entities'][wikidata_id]['sitelinks']['enwiki']['title']
                            en_url = f"https://en.wikipedia.org/wiki/{en_title.replace(' ', '_')}"

                            # Escape user-supplied content
                            escaped_title = escape(correct_title)
                            escaped_url = escape(original_url)

                            return f"<b>English Wikipedia page found for <a href=\"{escaped_url}\">{escaped_title}</a></b>:\n{en_url}"
                        else:
                            escaped_title = escape(correct_title)
                            escaped_url = escape(original_url)
                            return f"<b>No English Wikipedia page found for <a href=\"{escaped_url}\">{escaped_title}</a></b>."
    return None

async def process_link(session, original_url):
    # now this will correctly resolve to the global helper
    if not is_valid_domain(original_url):
        logger.warning("Blocked non‐wiki URL: %s", original_url)
        return None

    # Decode the URL to ensure special characters are handled properly
    decoded_url = unquote(original_url)
    match = re.search(r'https?://([a-z]{2,3})?\.?m?\.?wikipedia\.org/wiki/(.+)', decoded_url)
    if match:
        language_code, article_title_encoded = match.groups()
        if language_code == 'en':  # Skip English Wikipedia links
            return None
        # Decode article title for API calls/display
        article_title = unquote(article_title_encoded).replace('_', ' ')
        # Use the original (encoded) URL for consistency with external requests
        response = await get_english_wikipedia_url(session, original_url, article_title, language_code)
        if response:
            return f"{response}\n\n"
    return None

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query
    links = re.findall(r'https?://[^\s]+', query)
    unique_links = []
    seen_titles = set()

    for link in links:
        if 'wikipedia.org/wiki/' in link and not link.startswith('https://en.wikipedia.org/wiki/'):
            title = link.split('/wiki/')[-1]
            if title not in seen_titles:  # Check if the title has already been processed
                unique_links.append(link)
                seen_titles.add(title)

    if unique_links:
        async with aiohttp.ClientSession() as session:
            tasks = [process_link(session, url) for url in unique_links]
            responses = await asyncio.gather(*tasks)

        # Filter out None responses and aggregate
        aggregated_response = ''.join([resp for resp in responses if resp]).strip()

        if aggregated_response:
            results = [InlineQueryResultArticle(
                id=str(uuid4()),
                title="Wikipedia Links",
                input_message_content=InputTextMessageContent(aggregated_response, parse_mode='HTML', disable_web_page_preview=True),
                description="Aggregated English Wikipedia Links"
            )]
        else:
            results = [InlineQueryResultArticle(
                id=str(uuid4()),
                title="No Valid Links",
                input_message_content=InputTextMessageContent("No valid non-English Wikipedia page URL found."),
                description="No valid links processed"
            )]
    else:
        results = [InlineQueryResultArticle(
            id=str(uuid4()),
            title="No Non-English Links",
            input_message_content=InputTextMessageContent("Please enter non-English Wikipedia page URL(s)."),
            description="Only non-English Wikipedia links are processed"
        )]

    # Only process non-empty queries
    if query:
        async with aiohttp.ClientSession() as session:
            article_title = query
            original_url = f"https://en.wikipedia.org/wiki/{article_title.replace(' ', '_')}"
            response = await get_english_wikipedia_url(session, original_url, article_title, 'en')
            if response:
                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=article_title,
                        input_message_content=InputTextMessageContent(response),
                        description="Click to send this Wikipedia link"
                    )
                )
        await update.inline_query.answer(results, cache_time=10)

async def source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "You can find my source code here:\nhttps://github.com/jnton/english-wikipedia-link-converter-telegram-bot/\n\nFeel free to contribute or fork to create your own version!",
        reply_to_message_id=update.message.message_id
    )
    
async def license(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    license_text = (
        "<b>License</b>\n\n"
        "The code in this repository is licensed under the "
        "<a href=\"https://www.gnu.org/licenses/agpl-3.0.en.html\">GNU Affero General Public License v3.0 (AGPL-3.0)</a>, "
        "except where otherwise specified.\n\n"
        "The icon for the <b>English Wikipedia Link Converter</b> Telegram Bot is licensed under a "
        "<a href=\"http://creativecommons.org/licenses/by-sa/4.0/\">Creative Commons Attribution-ShareAlike 4.0 International License (CC BY-SA 4.0)</a>. "
        "See the <a href=\"https://github.com/jnton/english-wikipedia-link-converter-telegram-bot/tree/main/Telegram-Bot-Icon\">icon directory</a> for more details.\n\n"
        "<b>Image Credits</b>\n\n"
        "The bot's icon incorporates images from the following sources:\n\n"
        "- <b>Wikipedia logo</b>, Version2 by Vanished user 24kwjf10h32h, Version 1 by Nohat (concept by Paullusmagnus); Wikimedia., "
        "is used under a <a href=\"https://creativecommons.org/licenses/by-sa/3.0/\">Creative Commons Attribution-ShareAlike 3.0 Unported License (CC BY-SA 3.0)</a> "
        "and can be found <a href=\"https://commons.wikimedia.org/wiki/File:Wikipedia-logo-v2-square.svg\">here on Wikimedia Commons</a>.\n\n"
        "- <b>Left arrow</b>, by Icons8 is licensed under <a href=\"https://creativecommons.org/share-your-work/public-domain/cc0/\">CC0</a> "
        "and is available <a href=\"https://commons.wikimedia.org/wiki/File:Left-arrow_(61413)_-_The_Noun_Project.svg\">here on Wikimedia Commons</a>."
    )
    await update.message.reply_text(
        license_text,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_to_message_id=update.message.message_id
    )

async def send_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    bot = context.bot
    first_name = user.first_name

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
        "- Simply send me any non-English Wikipedia link, and I'll reply with the English version.\n"
        "- Add me to your group, and I'll automatically convert Wikipedia links shared by members.\n"
        f"- Use me in inline mode by typing <code>@{bot.username}</code> followed by the links to get instant conversions.\n"
    )

    # Create the inline keyboard button
    keyboard = [[InlineKeyboardButton("Add me to your group", url=f"https://t.me/{bot.username}?startgroup=true")]]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        description_text,
        parse_mode='HTML',
        reply_markup=reply_markup,
        reply_to_message_id=update.message.message_id

    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_info(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_info(update, context)

async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Privacy Policy:\n"
        "https://jnton.github.io/english-wikipedia-link-converter-telegram-bot/PRIVACY_POLICY.html",
        disable_web_page_preview=True,
        reply_to_message_id=update.message.message_id
    )

def setup_handlers(application):
    """Configure Telegram bot handlers for the application."""
    # Define command handlers and other message handlers
    start_handler = CommandHandler('start', start)
    help_handler = CommandHandler('help', help_command)
    check_wiki_link_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), check_wiki_link)
    inline_query_handler = InlineQueryHandler(inline_query)
    source_handler = CommandHandler('source', source)
    license_handler = CommandHandler('license', license)
    privacy_handler = CommandHandler("privacy", privacy)

    # Add handlers to the application
    application.add_handler(start_handler)
    application.add_handler(help_handler)
    application.add_handler(check_wiki_link_handler)
    application.add_handler(inline_query_handler)
    application.add_handler(source_handler)
    application.add_handler(license_handler)
    application.add_handler(privacy_handler)
    
async def async_lambda_handler(event, context):
    """Asynchronous handler for processing Telegram updates."""
    token = os.getenv("YOUR_TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Telegram bot token not found. Please set it in the Lambda environment variables.")
        return {'statusCode': 500, 'body': 'Bot token not found'}

    application = Application.builder().token(token).build()
    setup_handlers(application)

    if 'body' in event:
        try:
            update_data = json.loads(event['body'])
            update = Update.de_json(update_data, application.bot)
            await application.initialize()
            await application.process_update(update)
            await application.shutdown()
            return {'statusCode': 200, 'body': json.dumps({'message': 'Success'})}
        except Exception:
            logger.exception("An error occurred while processing the update.")
            return {'statusCode': 500, 'body': json.dumps({'error': 'Internal server error'})}
    else:
        logger.error("No update payload found.")
        return {'statusCode': 400, 'body': json.dumps({'message': 'No update payload'})}

def lambda_handler(event, context):
    # print("Raw event:", event)
    if 'body' not in event:
        logger.error("No 'body' in event. Event structure incorrect or missing data.")
        return {
            'statusCode': 400,
            'body': json.dumps({'message': 'Event structure incorrect or missing data'}),
            'headers': {'Content-Type': 'application/json'}
        }
    try:
        body = json.loads(event['body'])  # Correctly parse the JSON body from the event
        # print("Body parsed:", body)
    except json.JSONDecodeError as error:
        logger.error("Error decoding JSON.")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Invalid JSON received"})
        }

    return asyncio.run(async_lambda_handler(event, context))

# Ensure this part is not executed when the script is imported as a module in Lambda
#if __name__ == '__main__':
    # Run the bot normally if this script is executed locally
#    asyncio.run(main())
#    application = Application.builder().token("YOUR_TELEGRAM_BOT_TOKEN").build()  # Replace with your actual token
#    wiki_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), check_wiki_link)
#    inline_handler = InlineQueryHandler(inline_query)
#    source_handler = CommandHandler('source', source)
#    application.add_handler(source_handler)
#    application.add_handler(wiki_handler)
#    application.add_handler(inline_handler)
#    application.run_polling()
