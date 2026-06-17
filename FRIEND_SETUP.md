# NatSec News Bot Setup

This project is a Python Discord bot that reads RSS/Atom feeds and posts new articles into configured Discord channels. Runtime secrets and local state are intentionally not committed. Each machine needs its own `.env`, `config/config.json`, SQLite database, and logs.

## What You Need

- Git
- Docker Desktop, recommended for the simplest setup
- Or Python 3.13 for a native local run
- A Discord bot token
- The Discord server ID
- One or more Discord channel IDs where the bot can post

The Discord bot needs these permissions in the target channels:

- View Channel
- Send Messages
- Embed Links
- Use Slash Commands

Privileged message content intent is not required.

## Clone The Repo

```powershell
git clone https://github.com/Docbuchanan84/NatSec-News-Bot.git
cd NatSec-News-Bot
```

## Configure Local Files

```powershell
Copy-Item .env.example .env
Copy-Item config/config.example.json config/config.json
```

Edit `.env`:

```text
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=your_discord_server_id
CONFIG_PATH=config/config.json
DATABASE_PATH=data/rssbot.sqlite
LOG_LEVEL=INFO
```

Edit `config/config.json`:

- Replace every example `discordChannelId` with a real Discord channel ID.
- Add, remove, or rename channels under `channels`.
- Add RSS/Atom feeds under the top-level `feeds` list. Each feed should have a stable `id`, `sourceId`, `sourceClass`, `name`, `url`, and `routePolicy`.
- Use `legacyChannelKeys` only when a feed should still target a destination directly while routing is being tested.
- Routing rules live in `config/routing/`. Start with `settings.routing.mode` set to `observe_only`, validate the scoring, then switch to enforced routing when ready.
- Leave `postOldArticlesOnFirstRun` as `false` unless you want the bot to post older feed entries on first startup.

## Run With Docker Desktop

```powershell
docker volume create rssbot-data
docker compose run --rm rssbot python -m app.main --validate-config
docker compose run --rm rssbot python -m app.main --validate-routing
docker compose run --rm rssbot python -m app.main --routing-diagnostics
docker compose up -d --build
docker compose logs -f rssbot
```

Docker stores the runtime SQLite database in the external `rssbot-data` volume and writes logs to the local `logs` folder. Stop the log follow with `Ctrl+C`; the bot keeps running in the background. To stop the bot itself:

```powershell
docker compose stop rssbot
```

To run it again:

```powershell
docker compose up -d rssbot
```

For source code changes, rebuild before restarting:

```powershell
docker compose build rssbot
docker compose up -d --force-recreate rssbot
```

## Run Without Docker

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main --validate-config
python -m app.main --validate-routing
python -m app.main --routing-diagnostics
python -m app.main --init-db
python -m app.main
```

## Verify In Discord

After the bot is running:

```text
/rss status
/rss testpost
/rss refresh
```

If slash commands do not appear, confirm `DISCORD_GUILD_ID` is the correct server ID, confirm the bot was invited with slash command permissions, and restart the bot.

## Validate Before Handing Off

Run these checks from the repo root after editing config or routing:

```powershell
python -m app.main --validate-config --validate-env
python -m app.main --validate-routing
python -m app.main --routing-diagnostics
python -m app.main --route-backtest 25
pytest
```

## One-Shot Codex Prompt

Paste this into Codex from the folder where you want the project installed:

```text
Clone and set up this public GitHub project for me: https://github.com/Docbuchanan84/NatSec-News-Bot

I want to run it locally as a Codex-managed project. Please:
1. Clone the repository if it is not already present.
2. Read README.md and FRIEND_SETUP.md.
3. Check whether Docker Desktop and Python are available.
4. Create .env from .env.example and config/config.json from config/config.example.json if they do not exist.
5. Ask me for my Discord bot token, Discord server ID, and Discord channel IDs before writing secrets or live IDs.
6. Validate the config.
7. Validate routing with `--validate-routing` and `--routing-diagnostics`.
8. Run the tests with pytest.
9. Prefer Docker Compose for running the bot if Docker is available; otherwise set up a Python virtual environment and install requirements.txt.
10. Start the bot only after showing me the final local commands and confirming I am ready.

Do not commit my .env, config/config.json, data directory, logs, virtual environment, or other local runtime files.
```

## Common Problems

- `DISCORD_BOT_TOKEN is missing`: copy `.env.example` to `.env` and put in the real token.
- `Config file not found`: copy `config/config.example.json` to `config/config.json`.
- `discordChannelId must be a valid Discord channel ID`: replace placeholder IDs with real 17 to 20 digit Discord IDs.
- Nothing posts on first run: this is expected when `postOldArticlesOnFirstRun` is `false`; run `/rss refresh` after startup.
- A feed fails: run `/rss status` and check logs in the `logs` directory.
