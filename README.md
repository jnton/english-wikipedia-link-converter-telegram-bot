# English Wikipedia Link Converter

Telegram bot that converts any non-English Wikipedia article URL into its English equivalent: https://t.me/ToEnWikipediaBot.

## Reliability and security architecture

```text
Telegram webhook
      │
      ▼
AWS Lambda Function URL ── commands / inline queries ──► Telegram
      │
      └── link messages ──► Amazon SQS ──► dedicated worker Lambda
                                                │
                                                ├── Wikipedia API
                                                └── DynamoDB state / rate limits

EventBridge Scheduler ──► independent monitor Lambda
                              ├── Function URL health
                              ├── Telegram getWebhookInfo
                              ├── SQS / dead-letter queue
                              ├── SNS email alerts
                              └── optional Telegram status channel
```

The webhook acknowledges a conversion request only after SQS accepts it. SQS retries temporary failures and moves repeatedly failing updates to a dead-letter queue. DynamoDB provides update deduplication, processing leases, distributed abuse limits, recovery-notice suppression, and monitor state.

A separate monitor Lambda runs every 15 minutes, so the bot does not need to be healthy to report its own outage. `/status` displays the latest external check.

### Recovery behavior

To avoid flooding chats after an outage:

- group and channel messages older than 15 minutes are skipped;
- one recovery notice is sent per affected group during a six-hour recovery window;
- private requests 15–60 minutes old can be processed with a delay notice;
- at most three delayed private conversions are sent per chat during a six-hour recovery window;
- private requests older than one hour are skipped with one request-to-resend notice.

Telegram may redeliver webhook updates, and SQS/Lambda processing is at-least-once. The bot uses Telegram `update_id` values in DynamoDB to suppress duplicate processing.

## Abuse protection

- required Telegram webhook secret;
- POST-only webhook with JSON validation and a 64 KiB request limit;
- strict Wikipedia-domain validation;
- five links maximum per update;
- distributed limits of 10 conversion requests per user and 30 per chat per minute;
- separate distributed limits for commands and inline requests;
- rate-limit warnings deduplicated per chat;
- dedicated webhook and worker Lambda concurrency limits;
- SQS dead-letter handling and bounded retry behavior;
- short-lived GitHub OIDC deployment credentials;
- 14-day CloudWatch log retention;
- up to seven CloudWatch alarms and an optional AWS Budget, while keeping the account at or below ten standard alarms.

## AWS free-tier footprint

The deployment intentionally uses small, serverless resources:

- one 256 MB webhook Lambda with three reserved concurrent executions;
- one 256 MB SQS worker Lambda with two reserved concurrent executions;
- one 128 MB monitor Lambda invoked every 15 minutes;
- one standard SQS queue and one dead-letter queue;
- one DynamoDB table provisioned at 10 read and 10 write capacity units;
- one EventBridge Scheduler schedule;
- one SNS topic;
- up to seven CloudWatch alarms, automatically capped so the account stays at or below ten standard alarms;
- 14-day log retention.

Separating the SQS worker preserves webhook capacity during recovery and avoids configuring SQS maximum concurrency, allowing Lambda to reduce idle queue polling. This is designed to remain within the traditional AWS free allowances at normal small-bot traffic, but AWS pricing and account eligibility can vary. Set `ALERT_EMAIL` and the deployment will also create a small monthly budget alert when the account has a free budget slot.

## Deployment

The GitHub Actions workflow tests every pull request and every `main` deployment under Python 3.13, matching the Lambda runtime.

Required existing GitHub secrets:

- `AWS_REGION` — currently `eu-north-1`;
- `LAMBDA_FUNCTION_NAME` — currently `ToEnWikipediaBot`;
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` — required only for the first OIDC bootstrap;
- the Telegram bot token remains in the Lambda environment as `TELEGRAM_BOT_TOKEN` or the legacy `YOUR_TELEGRAM_BOT_TOKEN`.

Optional GitHub secrets:

- `ALERT_EMAIL` — SNS and AWS Budget alerts; confirm the SNS subscription email after deployment;
- `ADMIN_CHAT_ID` — private Telegram administrator alerts;
- `STATUS_CHANNEL_ID` — channel username or numeric ID for outage/recovery posts;
- `STATUS_CHANNEL_URL` — public link displayed by `/start`, `/help`, and `/status`;
- `STATUS_BOT_TOKEN` — optional separate bot used only to publish alerts. Without it, the main bot token is used.

Optional GitHub repository variable:

- `MONTHLY_BUDGET_USD` — defaults to `1`.

On the first successful deployment, the workflow creates a repository- and branch-restricted GitHub OIDC role. After that run succeeds, delete the long-lived `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` GitHub secrets; future deployments use short-lived credentials.

The deployment automatically:

1. deploys the code;
2. creates or updates SQS, DynamoDB, SNS, IAM, the dedicated worker and monitor Lambdas, schedule, alarms, log retention, and optional budget;
3. creates and stores a webhook secret without printing it;
4. points Telegram directly to the Lambda Function URL with `drop_pending_updates=false`;
5. limits Telegram to `message`, `channel_post`, and `inline_query` updates;
6. configures the BotFather command menu through `setMyCommands`;
7. removes legacy API Gateway invoke permission after the Function URL webhook is verified;
8. invokes the independent monitor and fails deployment only when the Function URL or Telegram webhook is not working; an existing queue backlog remains visible without blocking a repair deployment.

The old API Gateway resource may remain visible in AWS after its invoke permission is removed. It can be deleted manually once the Function URL webhook has been verified.

## Operations

- `/status` shows the most recent independent check, Telegram pending-update count, queue depth, and deployed version.
- CloudWatch alarms cover Function URL 5xx responses, webhook and worker Lambda errors, throttles, queue age, dead-letter messages, and monitor errors.
- The independent monitor additionally detects a mismatched webhook URL, a growing Telegram backlog, a stale SQS backlog, and dead-letter messages.
- Monitor notifications are transition-based, with six-hour reminders during a continuing outage, to avoid alert spam.

Do not store or log message text, user names, bot tokens, or webhook secrets. The persistent DynamoDB records contain operational keys and counters only and expire automatically, except for the current monitor status record.

## License

The code in this repository is licensed under the [GNU Affero General Public License v3.0 (AGPL-3.0)](https://www.gnu.org/licenses/agpl-3.0.en.html), except where otherwise specified.

The icon for the English Wikipedia Link Converter Telegram Bot is licensed under a [Creative Commons Attribution-ShareAlike 4.0 International License (CC BY-SA 4.0)][cc-by-sa]. See the [icon directory](https://github.com/JnTon/English-Wikipedia-Link-Converter-Telegram-Bot/tree/main/Telegram-Bot-Icon) for more details.

### Image credits

- **Wikipedia logo**, Version2 by Vanished user 24kwjf10h32h, Version 1 by Nohat (concept by Paullusmagnus); Wikimedia, used under [CC BY-SA 3.0][cc-by-sa-3.0].
- **Left arrow**, by Icons8, available under [CC0](https://creativecommons.org/share-your-work/public-domain/cc0/).

[cc-by-sa]: http://creativecommons.org/licenses/by-sa/4.0/
[cc-by-sa-3.0]: https://creativecommons.org/licenses/by-sa/3.0/
