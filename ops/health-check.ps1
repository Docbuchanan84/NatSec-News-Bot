param(
    [switch]$PassThru
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Continue"

Import-Module (Join-Path $PSScriptRoot "lib\RssBotOps.psm1") -Force

$repoRoot = Get-RssBotRepoRoot
$reportPath = Get-RssBotReportPath -Name "health-check"
$lines = [System.Collections.Generic.List[string]]::new()
$hadFailure = $false

$lines.Add("RSS Bot Host Health Check")
$lines.Add("Generated: $(Get-Date -Format o)")
$lines.Add("Repo: $repoRoot")

Write-RssBotSection -Title "Host" -Lines $lines
$os = Get-CimInstance Win32_OperatingSystem
$lines.Add("last_boot=$($os.LastBootUpTime)")
$lines.Add("local_time=$($os.LocalDateTime)")
$drive = Get-PSDrive -Name C
$freeGb = [math]::Round($drive.Free / 1GB, 2)
$usedGb = [math]::Round($drive.Used / 1GB, 2)
$lines.Add("c_used_gb=$usedGb")
$lines.Add("c_free_gb=$freeGb")
if ($freeGb -lt 30) {
    $lines.Add("WARNING: C: free space is below 30 GB.")
}

Add-RssBotCommandReport -Lines $lines -Title "Docker Compose Status" -Command @("docker", "compose", "ps") | Out-Null
$botRunning = Test-RssBotContainerRunning
$lines.Add("bot_running=$botRunning")
Add-RssBotCommandReport -Lines $lines -Title "Recent Bot Log Signals" -Command @(
    "powershell",
    "-NoProfile",
    "-Command",
    "docker compose logs --since 24h rssbot | Select-String -Pattern 'Connected to Discord|heartbeat blocked|disk I/O error|database disk image|Runtime DB maintenance|Feed failed' | Select-Object -Last 80"
) | Out-Null

Write-RssBotSection -Title "Runtime Database" -Lines $lines
if ($botRunning) {
    $lines.Add("offline_integrity_check=skipped")
    $lines.Add("reason=bot is running; avoid host/container SQLite contention during live writes")
} else {
    $integrityResult = Add-RssBotOfflineIntegrityReport -Lines $lines
    if ($integrityResult.ExitCode -ne 0) {
        $hadFailure = $true
    }
}

if ($botRunning) {
    Write-RssBotSection -Title "Feed Health" -Lines $lines
    $lines.Add("feed_health_report=skipped")
    $lines.Add("reason=bot is running; feed health reads the same SQLite DB")
} else {
    Add-RssBotAppCommandReport -Lines $lines -Title "Feed Health" -Arguments @(
        "--feed-health-report", "--min-feed-failures", "10"
    ) | Out-Null
}

$lines | Set-Content -Path $reportPath -Encoding UTF8
Write-Host "Health report written: $reportPath"
if ($PassThru) {
    Get-Content $reportPath
}
if ($hadFailure) {
    exit 1
}
