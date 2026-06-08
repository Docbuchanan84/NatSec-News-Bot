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

- `RSS Bot Daily Health Check`: daily at 9:00 AM.
- `RSS Bot Weekly Maintenance`: Sunday at 3:30 AM.
- `RSS Bot Post Reboot Check`: at user logon. If Windows denies logon-task registration, the setup script creates an equivalent Startup folder shortcut for the current user.

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
