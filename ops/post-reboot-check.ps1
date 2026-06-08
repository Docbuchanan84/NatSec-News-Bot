Set-StrictMode -Version 3.0
$ErrorActionPreference = "Continue"

Import-Module (Join-Path $PSScriptRoot "lib\RssBotOps.psm1") -Force
Set-Location (Get-RssBotRepoRoot)

$reportPath = Get-RssBotReportPath -Name "post-reboot-check"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("RSS Bot Post-Reboot Check")
$lines.Add("Generated: $(Get-Date -Format o)")

Add-RssBotCommandReport -Lines $lines -Title "Docker Status Before Start" -Command @("docker", "compose", "ps") | Out-Null
Add-RssBotCommandReport -Lines $lines -Title "Ensure Bot Is Up" -Command @("docker", "compose", "up", "-d", "rssbot") | Out-Null
Start-Sleep -Seconds 12
Add-RssBotCommandReport -Lines $lines -Title "Docker Status After Start" -Command @("docker", "compose", "ps") | Out-Null
Add-RssBotCommandReport -Lines $lines -Title "Startup Log Signals" -Command @(
    "powershell",
    "-NoProfile",
    "-Command",
    "docker compose logs --since 15m rssbot | Select-String -Pattern 'Connected to Discord|Routing config loaded|Configured|heartbeat blocked|disk I/O error|database disk image'"
) | Out-Null

$lines | Set-Content -Path $reportPath -Encoding UTF8
Write-Host "Post-reboot report written: $reportPath"
