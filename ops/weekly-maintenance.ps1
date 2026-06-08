param(
    [switch]$SkipWindowsRestart
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "lib\RssBotOps.psm1") -Force
Set-Location (Get-RssBotRepoRoot)

$reportPath = Get-RssBotReportPath -Name "weekly-maintenance"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("RSS Bot Weekly Maintenance")
$lines.Add("Generated: $(Get-Date -Format o)")
$hadFailure = $false

try {
    Write-RssBotSection -Title "Pre-check" -Lines $lines
    $healthOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "health-check.ps1") 2>&1
    foreach ($line in $healthOutput) {
        $lines.Add([string]$line)
    }

    $stopResult = Add-RssBotCommandReport -Lines $lines -Title "Stop Bot" -Command @("docker", "compose", "stop", "rssbot")
    if ($stopResult.ExitCode -ne 0) {
        $hadFailure = $true
    }

    $integrityBefore = Add-RssBotOfflineIntegrityReport -Lines $lines -Title "Pre-maintenance DB Integrity"
    if ($integrityBefore.ExitCode -ne 0) {
        $hadFailure = $true
        $lines.Add("CRITICAL: pre-maintenance DB integrity command failed; skipping DB maintenance.")
    } else {
        $maintenanceResult = Add-RssBotAppCommandReport -Lines $lines -Title "Database Maintenance" -Arguments @("--maintain-db")
        if ($maintenanceResult.ExitCode -ne 0) {
            $hadFailure = $true
        }
        $integrityAfter = Add-RssBotOfflineIntegrityReport -Lines $lines -Title "Post-maintenance DB Integrity"
        if ($integrityAfter.ExitCode -ne 0) {
            $hadFailure = $true
        }
    }
} catch {
    $hadFailure = $true
    Write-RssBotSection -Title "Weekly Maintenance Exception" -Lines $lines
    $lines.Add([string]$_)
} finally {
    $startResult = Add-RssBotCommandReport -Lines $lines -Title "Start Bot" -Command @("docker", "compose", "up", "-d", "rssbot")
    if ($startResult.ExitCode -ne 0) {
        $hadFailure = $true
    }
    Start-Sleep -Seconds 12
    Add-RssBotCommandReport -Lines $lines -Title "Post-start Status" -Command @("docker", "compose", "ps") | Out-Null
    Add-RssBotCommandReport -Lines $lines -Title "Post-start Log Signals" -Command @(
        "powershell",
        "-NoProfile",
        "-Command",
        "docker compose logs --since 10m rssbot | Select-String -Pattern 'Connected to Discord|Routing config loaded|Configured|heartbeat blocked|disk I/O error|database disk image|Runtime DB maintenance'"
    ) | Out-Null
}

if ($SkipWindowsRestart) {
    $lines.Add("")
    $lines.Add("Windows restart skipped because -SkipWindowsRestart was supplied.")
} elseif ($hadFailure) {
    $lines.Add("")
    $lines.Add("Windows restart skipped because weekly maintenance reported a failure.")
} else {
    $lines.Add("")
    $lines.Add("Requesting Windows restart in 60 seconds.")
}

$lines | Set-Content -Path $reportPath -Encoding UTF8
Write-Host "Weekly maintenance report written: $reportPath"

if ($hadFailure) {
    exit 1
}

if (-not $SkipWindowsRestart) {
    shutdown.exe /r /t 60 /c "RSS Bot weekly maintenance restart"
}
