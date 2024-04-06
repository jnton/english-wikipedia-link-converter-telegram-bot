import logging
import re
import aiohttp
import asyncio
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
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
    message = update.message.text
    wiki_link_regex = re.compile(r'https?://([a-z]{2,3})\.wikipedia\.org/wiki/(.+)')
    matches = wiki_link_regex.findall(message)
    unique_links = set(matches)  # Create a set of unique links to avoid processing duplicates
    
    if unique_links:
        responses = []
        async with aiohttp.ClientSession() as session:
            for match in unique_links:
                language_code, article_title = match
                original_url = f"https://{language_code}.wikipedia.org/wiki/{article_title}"
                if language_code == 'en':  # Skip English Wikipedia links
                    continue
                response = await get_english_wikipedia_url(session, original_url, article_title.replace('_', ' '), language_code)
                if response:
                    responses.append(response)
            
            if responses:
                sorted_responses = sorted(set(responses))  # Sort and remove duplicates
                reply_message = "\n\n".join(sorted_responses)
                await update.message.reply_text(reply_message, reply_to_message_id=update.message.message_id, parse_mode='HTML')

async def process_link(session, original_url):
    match = re.search(r'https?://([a-z]{2,3})\.wikipedia\.org/wiki/(.+)', original_url)
    if match:
        language_code, article_title = match.groups()
        if language_code == 'en':  # Skip English Wikipedia links
            return None
        article_title = re.sub('_', ' ', article_title)  # Decode URL-encoded article title
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
                input_message_content=InputTextMessageContent(aggregated_response, parse_mode='HTML'),
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
        "You can find my source code here: https://github.com/JnTon/WikipediaEnLinkBot\n\nFeel free to contribute or fork to create your own version!"
    )


if __name__ == '__main__':
    application = Application.builder().token("YOUR_TELEGRAM_BOT_TOKEN").build()  # Replace with your actual token
    wiki_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), check_wiki_link)
    inline_handler = InlineQueryHandler(inline_query)
    source_handler = CommandHandler('source', source)
    application.add_handler(source_handler)
    application.add_handler(wiki_handler)
    application.add_handler(inline_handler)
    application.run_polling()
