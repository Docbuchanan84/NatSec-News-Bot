Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "lib\RssBotOps.psm1") -Force

foreach ($taskName in Get-RssBotTaskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        Write-Host "Enabled task: $taskName"
    } else {
        Write-Host "Task not found: $taskName"
    }
}

$shortcutPath = Get-RssBotStartupShortcutPath
$disabledShortcutPath = "$shortcutPath.disabled"
if (Test-Path $disabledShortcutPath) {
    Rename-Item -Path $disabledShortcutPath -NewName "RSS Bot Post Reboot Check.lnk" -Force
    Write-Host "Enabled Startup shortcut: $shortcutPath"
}
