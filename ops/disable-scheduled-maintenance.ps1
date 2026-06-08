Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "lib\RssBotOps.psm1") -Force

foreach ($taskName in Get-RssBotTaskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        Write-Host "Disabled task: $taskName"
    } else {
        Write-Host "Task not found: $taskName"
    }
}

$shortcutPath = Get-RssBotStartupShortcutPath
if (Test-Path $shortcutPath) {
    Rename-Item -Path $shortcutPath -NewName "RSS Bot Post Reboot Check.lnk.disabled" -Force
    Write-Host "Disabled Startup shortcut: $shortcutPath"
}
