# Privacy Policy

_Last updated: 2025-05-31_

This Privacy Policy describes how the **English Wikipedia Link Converter Bot** ("we", "us", "Bot") collects, uses, and protects your information when you interact via Telegram.

---

## 1. Data We Collect  
- **Telegram user ID** (for rate-limiting).  
- **Message text** containing Wikipedia URLs.  
- **Temporary logs** (in memory only).

## 2. How We Use Your Data  
- Convert non-English Wikipedia links to their English equivalent.  
- Enforce a short-term, in-memory rate limit (e.g. 30 requests/60 s).  
- Log only warnings/errors (no persistent chat history).

## 3. Data Retention & Storage  
- All data is kept **in memory**.  
- No persistent database or external storage.  
- Data is discarded when the bot restarts.

## 4. Data Sharing & Third Parties  
- We **never** share your data with third parties.  
- No analytics, marketing, or ad networks.  
- Dependencies (e.g. `python-telegram-bot`, `aiohttp`) do not store user messages.

## 5. Your Rights  
Under applicable laws you may:  
- Request a copy of data we hold (none beyond ephemeral memory).  
- Request deletion (data is auto-deleted on restart).  
- Withdraw consent by stopping use of the Bot.

_To exercise rights or for any privacy inquiries, please open an issue at [https://github.com/jnton/english-wikipedia-link-converter-telegram-bot/issues](https://github.com/jnton/english-wikipedia-link-converter-telegram-bot/issues)._