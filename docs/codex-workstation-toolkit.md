# Codex Workstation Toolkit

This PC has a toolkit for maintaining the RSS Feed Bot and doing broader Codex-assisted development. Prefer non-destructive inspection commands first, especially when the bot is live.

## RSS Bot Essentials

- `docker`: Runs and inspects the bot container. Common commands: `docker compose ps`, `docker compose logs --since 5m rssbot`, `docker compose up -d rssbot`.
- `sqlite3`: Checks and queries SQLite databases. For this bot, inspect `/app/data/rssbot.sqlite` from inside Docker because the live database is in the `rssbot-data` volume.
- `sqlite3_analyzer`: Shows SQLite table and index space usage. Useful for finding bloat.
- `sqldiff`: Compares two SQLite databases.
- `DB Browser for SQLite`: GUI for offline database inspection. Use only on copied/offline DBs, not the live bot database.
- `handle` / Sysinternals Suite: Finds Windows processes holding files open. Useful for lock and disk diagnostics.

## Search, JSON, And Config

- `rg`: Fast text search across the repo.
- `fd`: Fast file finding.
- `jq`: Reads and transforms JSON from config files, Docker output, and APIs.
- `yq`: Reads and transforms YAML and can also inspect JSON.
- `bat`: Syntax-highlighted file viewer.
- `less`: Pager for long output.
- `fzf`: Interactive fuzzy picker for files, command output, and search results.

## Python And App Quality

- `uv`: Fast Python package and tool manager.
- `ruff`: Fast Python linting and formatting checks.
- `mypy`: Python type checking.
- `pre-commit`: Runs configured checks before commits when a repo opts in.
- `httpie`: Friendly HTTP client for API and feed checks.
- `pytest`: Runs the RSS bot test suite.

## Git And Review

- `gh`: GitHub CLI for repos, PRs, issues, and actions.
- `delta`: Better Git diff viewer. Git is configured to use it as the pager.
- `lazygit`: Terminal UI for inspecting Git history and changes.

## System And Performance

- `PowerShell 7`: Modern shell with better cross-platform behavior than Windows PowerShell 5.1.
- `7z`: Archives and extracts compressed files.
- `gdu`: Disk usage explorer.
- `eza`: Modern directory listing.
- `zoxide`: Fast directory jumping for interactive shells.
- `hyperfine`: Benchmarks command runtimes.
- `PowerToys`: Windows utilities such as search, window tools, and text extraction.

Some WinGet packages do not expose the expected executable names directly. Stable shims for `gdu` and `7z` live in `C:\Users\stacy\.local\bin`, which is already used by `uv` tools.

## Safe Defaults For This Bot

- Do not run host-side SQLite checks against the live bot database while the bot is writing.
- Use container-side checks for the named volume:

```powershell
docker compose run --rm --no-deps rssbot python -c "import sqlite3; c=sqlite3.connect('/app/data/rssbot.sqlite', timeout=30); print(c.execute('pragma quick_check').fetchone()[0]); print(len(c.execute('pragma foreign_key_check').fetchall())); c.close()"
```

- Use `docker compose build rssbot` after Python source changes.
- Use `docker compose up -d --force-recreate rssbot` after rebuilt code changes.
- Use `python -m pytest -q`, `python -m app.main --validate-config --validate-env`, and `python -m app.main --validate-routing` before runtime changes where practical.
