import os
import logging
import re
from urllib.parse import unquote
import aiohttp
import json
import asyncio
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent, MessageEntity
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes, InlineQueryHandler
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

logger = logging.getLogger(__name__)

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query
    results = []

    logger.info(f"Handling inline query: {query}")

    if query:
        try:
            async with aiohttp.ClientSession() as session:
                # Simplified response for debugging
                # This is where you would normally call `get_english_wikipedia_url`
                # For now, let's return a static result to ensure inline queries work
                static_response = "This is a static response for debugging."
                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid4()),
                        title="Debugging Title",
                        input_message_content=InputTextMessageContent(static_response),
                        description="Static response for testing"
                    )
                )
            await update.inline_query.answer(results, cache_time=10)
        except Exception as e:
            logger.error(f"Error handling inline query: {e}")

async def get_english_wikipedia_url(session, original_url, article_title, language_code):
    if language_code == 'en':
        return None
    wiki_api_url = f"https://{language_code}.wikipedia.org/w/api.php"
    params = {
        'action': 'query',
        'format': 'json',
        'titles': article_title.replace('_', ' '),  # Correctly handle spaces in titles
        'prop': 'pageprops|info',
        'inprop': 'url'  # Request the full URL to ensure we have it for reference
    }
    async with session.get(wiki_api_url, params=params) as response:
        if response.status == 200:
            data = await response.json()
            pages = next(iter(data['query']['pages'].values()))
            correct_title = pages.get('title', article_title.replace('_', ' '))  # Fetch the display title
            wikidata_id = pages.get('pageprops', {}).get('wikibase_item')
            if wikidata_id:
                wikidata_url = f"https://www.wikidata.org/wiki/Special:EntityData/{wikidata_id}.json"
                async with session.get(wikidata_url) as wd_response:
                    if wd_response.status == 200:
                        wd_data = await wd_response.json()
                        if 'enwiki' in wd_data['entities'][wikidata_id]['sitelinks']:
                            en_title = wd_data['entities'][wikidata_id]['sitelinks']['enwiki']['title']
                            en_url = f"https://en.wikipedia.org/wiki/{en_title.replace(' ', '_')}"
                            return f"<b>English Wikipedia page found for <a href=\"{original_url}\">{correct_title}</a></b>:\n{en_url}"
                        else:
                            return f"<b>No English Wikipedia page found for <a href=\"{original_url}\">{correct_title}</a></b>."
    return None

async def check_wiki_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    except Exception as e:
        logger.error(f"Error in check_wiki_link: {e}")
        logger.error(traceback.format_exc())

async def process_link(session, original_url):
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

    await update.inline_query.answer(results, cache_time=10)    
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
        "You can find my source code here: https://github.com/JnTon/English-Wikipedia-Link-Converter-Telegram-Bot\n\nFeel free to contribute or fork to create your own version!"
    )

def setup_handlers(application):
    """ Configure Telegram bot handlers for the application. """
    # Define command handlers and other message handlers
    check_wiki_link_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), check_wiki_link)
    inline_query_handler = InlineQueryHandler(inline_query)

    # Add handlers to the application
    application.add_handler(check_wiki_link_handler)
    application.add_handler(inline_query_handler)

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
        except Exception as e:
            logger.error(f"An error occurred while processing the update: {e}")
            return {'statusCode': 500, 'body': json.dumps({'error': 'Internal server error'})}
    else:
        logger.error("No update payload found.")
        return {'statusCode': 400, 'body': json.dumps({'message': 'No update payload'})}

def lambda_handler(event, context):
    print("Raw event:", event)
    try:
        body = json.loads(event['body'])  # Correctly parse the JSON body from the event
        print("Body parsed:", body)
    except json.JSONDecodeError as error:
        print("Error decoding JSON:", error)
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Invalid JSON received"})
        }

    if 'body' not in event:
        logger.error("No 'body' in event. Event structure incorrect or missing data.")
        return {
            'statusCode': 400,
            'body': json.dumps({'message': 'Event structure incorrect or missing data'}),
            'headers': {'Content-Type': 'application/json'}
        }

    # Ensure to use the correct variable name that contains the parsed JSON
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
