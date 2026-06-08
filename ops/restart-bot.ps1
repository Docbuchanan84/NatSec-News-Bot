Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "lib\RssBotOps.psm1") -Force
Set-Location (Get-RssBotRepoRoot)

docker compose up -d --force-recreate rssbot
Start-Sleep -Seconds 8
docker compose ps
docker compose logs --since 5m rssbot | Select-String -Pattern "Connected to Discord|Routing config loaded|Configured|heartbeat blocked|disk I/O error|database disk image"
