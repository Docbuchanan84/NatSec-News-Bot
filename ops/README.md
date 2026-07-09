# RSS Bot Host Operations

These scripts maintain the Windows host that runs the Dockerized RSS bot.
The runtime SQLite database lives in the Docker named volume `rssbot-data`.
Do not run host-side Python directly against a live SQLite file while the bot is running.
For the broader local CLI/tooling reference, see `docs/codex-workstation-toolkit.md`.

If the volume ever needs to be created manually:

```powershell
docker volume create rssbot-data
```

Run commands from the repo root in PowerShell.

## Check It

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\health-check.ps1 -PassThru
```

Reports are written to `ops\reports\`, which is ignored by git.

## Turn Bot Off

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\stop-bot.ps1
```

This stops only the `rssbot` container. It does not shut down Docker Desktop or Windows.

## Turn Bot On

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\start-bot.ps1
```

## Restart Bot

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\restart-bot.ps1
```

## Register Scheduled Maintenance

Dry run first:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\register-scheduled-tasks.ps1 -WhatIf
```

Register the tasks:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\register-scheduled-tasks.ps1
```

Default tasks:

- `RSS Bot Draft Worker Watchdog`: every 5 minutes. Starts the host-side worker used by `/rss draft-post` and importance review if it is not already running. Drafts use the configured OpenAI or Codex backend; importance review remains on Codex.
- `RSS Bot Daily Health Check`: daily at 9:00 AM.
- `RSS Bot Weekly Maintenance`: Sunday at 3:30 AM.
- `RSS Bot Post Reboot Check`: at user logon. Ensures the bot container and draft worker are up. If Windows denies logon-task registration, the setup script creates an equivalent Startup folder shortcut for the current user.

## Draft Worker

Copy `.env.openai.example` to the ignored `.env.openai`, add the project API key, and select the live backend:

```text
OPENAI_API_KEY=replace_with_project_api_key
DRAFT_BACKEND=openai
```

Start or stop the worker:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\start-draft-worker.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\stop-draft-worker.ps1
```

Set `DRAFT_BACKEND=codex` in `.env.openai` and restart the worker for an immediate rollback. `/importance-review` uses Codex under either setting.

OpenAI draft metrics are written to ignored `logs\openai-draft-metrics.jsonl`. Summarize latency, usage, and estimated cost with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\openai-draft-cost-report.ps1
```

## Pause Or Resume Automation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\disable-scheduled-maintenance.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\enable-scheduled-maintenance.ps1
```

These toggle only the RSS bot scheduled tasks.
They also toggle the RSS bot Startup shortcut if Task Scheduler required that fallback.

## Manual Weekly Maintenance

Run without rebooting Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\weekly-maintenance.ps1 -SkipWindowsRestart
```

Run with the planned Windows restart:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\weekly-maintenance.ps1
```

## Warning Signs

Investigate before leaving the rig unattended if a report shows:

- `CRITICAL`
- `quick_check` is not `ok`
- `foreign_key_check_rows` is not `0`
- `disk I/O error`
- `database disk image is malformed`
- repeated `heartbeat blocked`
- C: free space below 30 GB
