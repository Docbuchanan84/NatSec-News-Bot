# Discord RSS Dispatch Bot

A portable Python 3.13 Discord bot that watches RSS/Atom feeds and posts new articles into configured Discord channels. Operators edit two files during normal use:

- `.env` for secrets and local paths
- `config/config.json` for channels, feeds, polling, dedupe, timestamp, and publishing behavior

Sharing this project with another operator? Send them `FRIEND_SETUP.md` after cloning the repo. It includes a full local setup walkthrough and a one-shot Codex prompt. If they should run your current live source/channel setup, also send the local-only `.env` and `config/config.json` files manually; those files are intentionally ignored by Git and are not on GitHub.

The config is feed-first: top-level `feeds` are inputs, and `channels` are Discord destinations selected by routing. Legacy channel-scoped feeds are still accepted for compatibility.

Structured routing is configured in `config/routing/` and documented in `docs/routing.md`. The example config enables routing in `observe_only` mode so operators can validate scoring before switching to enforced routing.

## Quick Start With Docker Desktop

1. Start Docker Desktop and wait for the engine to finish starting.

2. Create local runtime files. If someone gave you private runtime files, place them at `.env` and `config/config.json`. Otherwise copy the public examples:

   ```powershell
   Copy-Item .env.example .env
   Copy-Item config/config.example.json config/config.json
   ```

3. Edit `.env`:

   ```text
   DISCORD_BOT_TOKEN=your_bot_token
   DISCORD_GUILD_ID=your_server_id
   ```

4. Edit `config/config.json` and replace the example channel IDs with real Discord channel IDs. The public example config uses placeholder feeds; use the private `config/config.json` handoff file or add real RSS/Atom feed URLs before expecting production posts.

5. Validate the config:

   ```powershell
   docker volume create rssbot-data
   docker compose run --rm rssbot python -m app.main --validate-config --validate-env
   docker compose run --rm rssbot python -m app.main --validate-routing
   docker compose run --rm rssbot python -m app.main --routing-diagnostics
   ```

6. Start the bot:

   ```powershell
   docker compose up -d --build
   docker compose logs -f rssbot
   ```

Under Docker, the SQLite database lives in the `rssbot-data` Docker volume and survives restarts.
Create it once before the first Docker run if it does not already exist. The Compose file marks this volume as external so local state is never destroyed by recreating the service:

```powershell
docker volume create rssbot-data
```

Detailed audit and error logs can be enabled in `config/config.json` under `settings.logging`.

## Ingest Scheduling

RSS feeds and email sources run in independent async scheduler lanes. A slow RSS batch does not block email polling, and a slow email mailbox does not block RSS polling. Fetched items from all source types then flow through a shared result processor so dedupe, routing, database writes, and Discord publishing keep using the same article pipeline.

Tune RSS fetch parallelism with `settings.polling.maxConcurrentFeedFetches`. Tune email fetch parallelism separately with `settings.polling.maxConcurrentEmailFetches` (default `4`) so mailbox checks can stay fast without changing RSS pressure on external servers.

Tune shared post-processing throughput with `settings.polling.resultProcessorWorkers` (default `2`). Keep `settings.polling.backlogDrainEnabled` enabled when the bot should immediately continue through overdue feed batches after a long restart or slow polling cycle.

Routing can use longer article bodies than Discord display embeds. RSS `content`/`content:encoded` fields, email article bodies, and supported document extracts are stored in `rich_metadata.routing_summary` up to `settings.routing.maxRoutingSummaryChars` (default `2000`) so channel scoring can see useful context without making embeds noisy.

Routed posts also carry a local importance score from `0` to `10`. The score is persisted with the routing decision and displayed in the Discord embed footer as `Imp N`, with color-coding so high-impact active-conflict, attack, strategic-weapons, disaster, cyber, or official-source items stand out without changing the routing destination. Discord's native embed timestamp is used so each reader sees the post time in their own local time.

## Discord Post Formatting

Posts from RSS, email, Bluesky, X/social link enrichment, and other source-specific fetchers share the same Discord presentation path. When a source exposes direct image or playable video media, the bot temporarily downloads the media and uploads it to Discord as message attachments above the text embed. This avoids bare media URLs in the message body and keeps multi-image posts visually consistent across source types.

The media pipeline only uploads URLs that look suitable for Discord attachment playback or display. Direct image URLs are accepted broadly, while video uploads are limited to direct playable files such as `.mp4`, `.m4v`, `.mov`, and `.webm`. Non-direct video pages, such as YouTube watch URLs, are left as Discord-native link previews instead of being downloaded.

Embed footers use the compact format `Source · New/Update · Imp N`. Timestamps are stored on the embed itself rather than in the footer, because Discord footer text does not render timestamp markdown.

## Native Local Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item config/config.example.json config/config.json
python -m app.main --validate-config
python -m app.main --validate-routing
python -m app.main
```

## Adding A Feed

Open `config/config.json` and add a feed object to the top-level `feeds` array:

```json
{
  "id": "new-feed",
  "sourceId": "new-feed",
  "sourceClass": "major_media",
  "name": "New Feed Name",
  "url": "https://example.com/rss",
  "pollIntervalSeconds": 300,
  "initialBackfillHours": 24,
  "routePolicy": "normal",
  "routingTags": [],
  "legacyChannelKeys": [],
  "mirrorChannelKeys": []
}
```

Then run this in Discord:

```text
/rss reload
```

If the config is invalid, the bot reports the errors and keeps the previous working config active.

Routing destinations are configured separately. Add the destination channel under `channels`, then update `config/routing/channels.json` when the channel should receive routed stories by topic, source, or concept. Keep `settings.routing.mode` as `observe_only` while validating a new routing setup, then switch to enforced routing once `/rss route-test`, `/rss route-backtest`, and local validation look correct.

For new feeds, keep `initialBackfillHours` at `24` unless you intentionally want a tighter first-run window. Feed-level `routingTags` are optional routing hints for tightly scoped sources, such as a maritime-only or cyber-only feed; avoid broad tags on general news feeds.

Use `mirrorChannelKeys` for feeds that should always copy routed or reviewable items to a stable archive channel after routing has selected the primary destination. Mirror keys are not used for `no_match` items, so they do not bypass the router.

MSCIO document-folder URLs are supported for UKMTO/JMIC-style maritime security products. The bot parses folder rows and extracts the first pages of each PDF so Discord output and routing summaries include warning number, report time, location, incident detail, and advice when present. This requires `pypdf`, included in `requirements.txt`.

## Slash Commands

- `/rss status` shows uptime, configured channels, unique feeds, queue sizes, and recent feed health.
- `/rss reload` validates and reloads `config/config.json` without restarting.
- `/rss refresh` refreshes feeds for the Discord channel where the command is run.
- `/rss testpost` sends one controlled test embed in the current configured channel.
- `/rss route-test` previews routing for a supplied title and optional summary/source/source ID/source class/url.
- `/rss route-article` previews routing for an article already stored in SQLite.
- `/rss route-backtest` runs routing against recent SQLite articles.
- `/rss routing-status` shows routing mode, versions, rule counts, and validation status.
- `/rss explain` shows the latest persisted routing decision for an article, including routing details used for review/debug workflows.

## Long-Running Maintenance

Runtime maintenance is configured under `settings.maintenance`. The bot periodically prunes old non-post records, trims duplicate tracking tables, and runs SQLite `PRAGMA optimize` without changing the 5-minute polling cadence. For an operator-triggered cleanup:

```powershell
python -m app.main --maintain-db
python -m app.main --maintain-db --vacuum-db
python -m app.main --feed-health-report --min-feed-failures 10
```

Use `--vacuum-db` only while the bot is stopped or during a planned restart; it can briefly lock and rewrite the SQLite file.

Repeated feed failures use `settings.failureBackoff` to reduce wasted fetch attempts. By default, feeds with 10 consecutive failures wait at least 6 hours, feeds with 100 wait at least 24 hours, and feeds that have never succeeded after 500 failures wait a week before retrying. Set a feed's `routePolicy` to `"ignore"` to quarantine it entirely while keeping the config entry for later repair.

## Bot Permissions

The bot needs:

- View Channel
- Send Messages
- Embed Links
- Use Slash Commands

Privileged message content intent is not required.

## Design Notes

- The same feed URL can appear more than once. The bot normalizes feed URLs, fetches each unique feed once, and routes new articles to final destinations.
- First run defaults to suppressing old visible feed entries. They are marked seen so the bot does not dump a backlog into Discord.
- Articles are deduplicated globally using normalized URLs, feed GUIDs, and normalized title/source fingerprints.
- `channel_posts` enforces one successful post per article per Discord channel.
- Routing decisions store an importance score and reason list; Discord embeds show new/update state and compact `Imp N` importance in the footer, while the native embed timestamp shows viewer-local time.
- Supported source media is uploaded as Discord attachments above the embed instead of being rendered as bare image or video URLs.
- Feed and email fetching are asynchronous with bounded per-source concurrency and per-source timeouts.
- RSS and email polling use independent scheduler lanes and a shared result processor, so one source class does not wait behind another during normal operation.
- Source mirrors configured with `mirrorChannelKeys` are added only after routing or review selection, not for no-match items.
- Supported document-folder feeds can enrich summaries from PDF content for better routing and review formatting.
- The RSS scheduler reuses one HTTP session during normal operation to reduce connection churn.
- Feed health writes are recorded once per completed fetch instead of once at attempt start plus once at completion.
- Chronic feed failures back off automatically so dead RSS endpoints do not keep consuming fetch slots every cycle.
- Publishing uses one queue per configured Discord channel so one busy channel does not block another.
- Shutdown drains queued publisher work for `settings.publishing.shutdownDrainSeconds` before worker tasks are cancelled.
- RSS timestamps are treated as untrusted. The bot stores raw and normalized timestamps and corrects missing, invalid, timezone-naive, or future timestamps.
- Optional audit logging writes detailed runtime events to `logs/rssbot-audit.log` and errors to `logs/rssbot-errors.log`.

## Client Smoke Test

1. Create one private test channel in Discord.
2. Put that channel ID into `config/config.json`.
3. Use one known feed such as `https://www.cbsnews.com/latest/rss/world`.
4. Start the bot with Docker Compose.
5. Run `/rss status`.
6. Run `/rss testpost` in the test channel.
7. Run `/rss refresh` in the test channel.
8. Restart the bot and run `/rss refresh` again. The same old entries should not repost.

## Troubleshooting

- `Invalid JSON`: check commas, quotes, and brackets in `config/config.json`.
- `DISCORD_BOT_TOKEN is missing`: copy `.env.example` to `.env` and add the real token.
- Docker cannot connect to `dockerDesktopLinuxEngine`: start Docker Desktop and wait until `docker info` succeeds, then rerun the Compose command.
- Slash commands do not appear: confirm `DISCORD_GUILD_ID` is the server ID and restart the bot.
- First run only posts recent backfill: when `postOldArticlesOnFirstRun` is `false`, valid feed timestamps under the feed's `initialBackfillHours` window can post, while stale or undated entries are skipped or suppressed. Feeds default to 24 hours.
- A feed fails: check `/rss status` and logs for timeout, HTTP status, or parse errors.

## Development

Run tests:

```powershell
pytest
```

Validate routing config:

```powershell
python -m app.main --validate-routing
python -m app.main --routing-diagnostics
```

Preview routing locally:

```powershell
python -m app.main --route-test-title "Chinese carrier Liaoning enters Philippine Sea"
python -m app.main --route-backtest 25
```

Initialize the database only:

```powershell
python -m app.main --init-db
```
