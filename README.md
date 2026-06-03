# Discord RSS Dispatch Bot

A portable Python 3.13 Discord bot that watches RSS/Atom feeds and posts new articles into configured Discord channels. Operators edit two files during normal use:

- `.env` for secrets and local paths
- `config/config.json` for channels, feeds, polling, dedupe, timestamp, and publishing behavior

Sharing this project with another operator? Send them `FRIEND_SETUP.md` after cloning the repo. It includes a full local setup walkthrough and a one-shot Codex prompt.

The config is channel-first: each Discord channel declares the feeds it watches.

Structured routing is available as a safe first-pass classifier. It is configured in `config/routing/` and documented in `docs/routing.md`. The default example config leaves routing disabled; enable `settings.routing.enabled` with `mode: "observe_only"` before trying enforced routing.

## Quick Start With Docker Desktop

1. Copy the example files:

   ```powershell
   Copy-Item .env.example .env
   Copy-Item config/config.example.json config/config.json
   ```

2. Edit `.env`:

   ```text
   DISCORD_BOT_TOKEN=your_bot_token
   DISCORD_GUILD_ID=your_server_id
   ```

3. Edit `config/config.json` and replace the example channel IDs with real Discord channel IDs.

4. Validate the config:

   ```powershell
   docker compose run --rm rssbot python -m app.main --validate-config
   ```

5. Start the bot:

   ```powershell
   docker compose up --build
   ```

The SQLite database lives in `data/rssbot.sqlite` and survives restarts.
Detailed audit and error logs can be enabled in `config/config.json` under `settings.logging`.

## Native Local Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item config/config.example.json config/config.json
python -m app.main --validate-config
python -m app.main
```

## Adding A Feed

Open `config/config.json`, find the channel, and add a feed object to that channel's `feeds` array:

```json
{
  "name": "New Feed Name",
  "url": "https://example.com/rss"
}
```

Then run this in Discord:

```text
/rss reload
```

If the config is invalid, the bot reports the errors and keeps the previous working config active.

## Slash Commands

- `/rss status` shows uptime, configured channels, unique feeds, queue sizes, and recent feed health.
- `/rss reload` validates and reloads `config/config.json` without restarting.
- `/rss refresh` refreshes feeds for the Discord channel where the command is run.
- `/rss testpost` sends one controlled test embed in the current configured channel.
- `/rss route-test` previews routing for a supplied title and optional summary/source/url.
- `/rss route-article` previews routing for an article already stored in SQLite.
- `/rss route-backtest` runs routing against recent SQLite articles.
- `/rss routing-status` shows routing mode, versions, rule counts, and validation status.

## Bot Permissions

The bot needs:

- View Channel
- Send Messages
- Embed Links
- Use Slash Commands

Privileged message content intent is not required.

## Design Notes

- The same feed URL can appear under multiple Discord channels. The bot normalizes feed URLs, fetches each unique feed once, and fans out new articles to subscribed channels.
- First run defaults to suppressing old visible feed entries. They are marked seen so the bot does not dump a backlog into Discord.
- Articles are deduplicated globally using normalized URLs, feed GUIDs, and normalized title/source fingerprints.
- `channel_posts` enforces one successful post per article per Discord channel.
- Feed fetching is asynchronous with bounded concurrency and per-feed timeouts.
- Publishing uses one queue per configured Discord channel so one busy channel does not block another.
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
- Slash commands do not appear: confirm `DISCORD_GUILD_ID` is the server ID and restart the bot.
- First run only posts recent backfill: when `postOldArticlesOnFirstRun` is `false`, valid feed timestamps under `maxPostAgeHours` can post, while stale or undated entries are skipped or suppressed.
- A feed fails: check `/rss status` and logs for timeout, HTTP status, or parse errors.

## Development

Run tests:

```powershell
pytest
```

Validate routing config:

```powershell
python -m app.main --validate-routing
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
