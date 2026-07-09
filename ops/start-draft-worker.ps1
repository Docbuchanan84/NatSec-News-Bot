param(
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Worker = Join-Path $PSScriptRoot "draft-worker.py"
$LogPath = Join-Path $LogDir "draft-worker.log"
$ErrorLogPath = Join-Path $LogDir "draft-worker.err.log"

$existing = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*draft-worker.py*" -and $_.CommandLine -like "*--port $Port*" }

if ($existing) {
    "Draft worker already running on port $Port (PID $($existing.ProcessId -join ', '))."
    return
}

$argsList = @(
    "`"$Worker`"",
    "--host", $HostAddress,
    "--port", "$Port",
    "--repo", "`"$RepoRoot`""
)

Start-Process -FilePath "python" -ArgumentList $argsList -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrorLogPath
Start-Sleep -Seconds 2

try {
    $response = Invoke-RestMethod -Uri "http://$HostAddress`:$Port/health" -TimeoutSec 5
    if ($response.ok) {
        if ($response.draft_backend -eq "openai" -and (-not $response.openai_configured -or -not $response.openai_sdk_available)) {
            throw "OpenAI backend selected but key or SDK is unavailable."
        }
        "Draft worker started on http://$HostAddress`:$Port/draft"
        "Draft backend: $($response.draft_backend)"
        "Importance backend: $($response.importance_backend)"
        "Log: $LogPath"
        "Error log: $ErrorLogPath"
        return
    }
    "Worker start attempted, but health check returned an unexpected response."
    "Log: $LogPath"
    "Error log: $ErrorLogPath"
    exit 1
} catch {
    "Worker start attempted, but health check failed: $($_.Exception.Message)"
    "Log: $LogPath"
    "Error log: $ErrorLogPath"
    exit 1
}
