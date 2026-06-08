Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "lib\RssBotOps.psm1") -Force
Set-Location (Get-RssBotRepoRoot)

docker compose stop rssbot
docker compose ps
