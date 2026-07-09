param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$workers = Get-CimInstance Win32_Process |
    Where-Object {
        ($_.CommandLine -like "*draft-worker.py*" -or $_.CommandLine -like "*codex-draft-worker.py*") -and
        $_.CommandLine -like "*--port $Port*"
    }

if (-not $workers) {
    "No draft worker is running on port $Port."
    return
}

foreach ($worker in $workers) {
    Stop-Process -Id $worker.ProcessId -Force
    "Stopped draft worker PID $($worker.ProcessId)."
}
