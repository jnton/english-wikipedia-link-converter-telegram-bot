# Privacy Policy

_Last updated: 2025-06-01_

This Privacy Policy describes how the **English Wikipedia Link Converter Bot** ("we", "us", "Bot") collects, uses, and protects your information when you interact via Telegram.

---

### Standard Telegram Bot Privacy Policy  
This document supplements Telegram’s [Standard Bot Privacy Policy](https://telegram.org/privacy-tpa).  
Please refer to that for the general terms and definitions that apply to all bots on the platform.

---

## 0. Terms & Definitions
- **Bot / Third-Party Service**: English Wikipedia Link Converter Bot.
- **User**: Anyone interacting with the Bot via Telegram.

### Disclaimer
This Bot is an independent service and is neither maintained, endorsed, nor affiliated with Telegram Messenger Inc. Use of the Bot constitutes acceptance of this policy. If you do not agree, please stop using the Bot.

## 1. Data We Collect
- **Telegram user ID** (for rate-limiting).
- **Message text** containing Wikipedia URLs.
- **Temporary logs** (in memory only).

## 2. How We Use Your Data
- Convert non-English Wikipedia links to their English equivalents.
- Enforce a short-term, in-memory rate limit (30 requests per 60 s).
- Log only warnings/errors (no persistent chat history).

## 3. Third-Party Services
- We forward only user-supplied URLs or titles to the public Wikipedia API (no other personal data).
- Bot is deployed on AWS Lambda; AWS may process our logs but we do not persist any user message there.
- Telegram’s Bot API handles message delivery; we never share your user ID or text with any other party.

## 4. Data Retention & Storage
- All data is kept **in memory**; discarded on restart or after the 60 s rate-limit window.
- No database or external storage.

## 5. Data Sharing & Security
- We **never** sell or share your data with third parties.
- Communications with Wikipedia API and Telegram API occur over HTTPS.

## 6. Your Rights  
Under applicable laws you may:  
- Request a copy of data we hold (none beyond ephemeral memory).  
- Request deletion (data is auto-deleted on restart).  
- Withdraw consent by stopping use of the Bot.

_To exercise rights or for any privacy inquiries, please open an issue at [https://github.com/jnton/english-wikipedia-link-converter-telegram-bot/issues](https://github.com/jnton/english-wikipedia-link-converter-telegram-bot/issues)._