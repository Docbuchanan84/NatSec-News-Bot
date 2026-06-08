param(
    [switch]$WhatIf
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "lib\RssBotOps.psm1") -Force

$repoRoot = Get-RssBotRepoRoot
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$healthActionText = "`"powershell.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$repoRoot\ops\health-check.ps1`""
$weeklyActionText = "`"powershell.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$repoRoot\ops\weekly-maintenance.ps1`""
$postRebootActionText = "`"powershell.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$repoRoot\ops\post-reboot-check.ps1`""

$tasks = @(
    @{
        Name = "RSS Bot Daily Health Check"
        Trigger = New-ScheduledTaskTrigger -Daily -At 9:00am
        Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repoRoot\ops\health-check.ps1`""
        SchtasksArgs = @("/Create", "/TN", "RSS Bot Daily Health Check", "/SC", "DAILY", "/ST", "09:00", "/TR", $healthActionText, "/F")
    },
    @{
        Name = "RSS Bot Weekly Maintenance"
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3:30am
        Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repoRoot\ops\weekly-maintenance.ps1`""
        SchtasksArgs = @("/Create", "/TN", "RSS Bot Weekly Maintenance", "/SC", "WEEKLY", "/D", "SUN", "/ST", "03:30", "/TR", $weeklyActionText, "/F")
    },
    @{
        Name = "RSS Bot Post Reboot Check"
        Trigger = New-ScheduledTaskTrigger -AtLogOn
        Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repoRoot\ops\post-reboot-check.ps1`""
        SchtasksArgs = @("/Create", "/TN", "RSS Bot Post Reboot Check", "/SC", "ONLOGON", "/TR", $postRebootActionText, "/F")
    }
)

foreach ($task in $tasks) {
    if ($WhatIf) {
        Write-Host "Would register task: $($task.Name)"
        continue
    }
    try {
        Register-ScheduledTask -TaskName $task.Name -Action $task.Action -Trigger $task.Trigger -Principal $principal -Force | Out-Null
        Write-Host "Registered task: $($task.Name)"
    } catch {
        if ($task.Name -eq "RSS Bot Post Reboot Check") {
            Write-Host "Register-ScheduledTask failed for $($task.Name); creating Startup shortcut instead"
            $shortcutPath = Get-RssBotStartupShortcutPath
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = "powershell.exe"
            $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$repoRoot\ops\post-reboot-check.ps1`""
            $shortcut.WorkingDirectory = $repoRoot
            $shortcut.Save()
            Write-Host "Created Startup shortcut: $shortcutPath"
        } else {
            Write-Host "Register-ScheduledTask failed for $($task.Name); retrying with schtasks.exe"
            & schtasks.exe @($task.SchtasksArgs) | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "schtasks.exe failed for $($task.Name) with exit code $LASTEXITCODE"
            }
        }
    }
}
