Set-StrictMode -Version 3.0

function Get-RssBotRepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Get-RssBotReportPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )
    $repoRoot = Get-RssBotRepoRoot
    $reportsDir = Join-Path $repoRoot "ops\reports"
    New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    return Join-Path $reportsDir "$timestamp-$Name.txt"
}

function Invoke-RssBotCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Command,

        [string]$WorkingDirectory = (Get-RssBotRepoRoot)
    )
    Push-Location $WorkingDirectory
    try {
        $output = & $Command[0] @($Command | Select-Object -Skip 1) 2>&1
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) {
            $exitCode = 0
        }
        [pscustomobject]@{
            ExitCode = $exitCode
            Output = @($output)
        }
    } finally {
        Pop-Location
    }
}

function Write-RssBotSection {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Title,

        [Parameter(Mandatory = $true)]
        [object]$Lines
    )
    $Lines.Add("")
    $Lines.Add("=== $Title ===")
}

function Add-RssBotCommandReport {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Lines,

        [Parameter(Mandatory = $true)]
        [string]$Title,

        [Parameter(Mandatory = $true)]
        [string[]]$Command
    )
    Write-RssBotSection -Title $Title -Lines $Lines
    $Lines.Add("> $($Command -join ' ')")
    $result = Invoke-RssBotCommand -Command $Command
    $Lines.Add("exit_code=$($result.ExitCode)")
    foreach ($line in $result.Output) {
        $Lines.Add([string]$line)
    }
    return $result
}

function Test-RssBotContainerRunning {
    Push-Location (Get-RssBotRepoRoot)
    try {
        $running = docker inspect rss-discord-bot --format "{{.State.Running}}" 2>$null
        return ($LASTEXITCODE -eq 0 -and ([string]$running).Trim() -eq "true")
    } finally {
        Pop-Location
    }
}

function Add-RssBotAppCommandReport {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Lines,

        [Parameter(Mandatory = $true)]
        [string]$Title,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )
    $command = @("docker", "compose", "run", "--rm", "--no-deps", "rssbot", "python", "-m", "app.main") + $Arguments
    return Add-RssBotCommandReport -Lines $Lines -Title $Title -Command $command
}

function Add-RssBotOfflineIntegrityReport {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Lines,

        [string]$Title = "Runtime Database Integrity"
    )
    $python = "import sqlite3; path='/app/data/rssbot.sqlite'; conn=sqlite3.connect(path, timeout=30); print('path=' + path); print('quick_check=' + conn.execute('PRAGMA quick_check').fetchone()[0]); print('foreign_key_check_rows=' + str(len(conn.execute('PRAGMA foreign_key_check').fetchall()))); print('journal_mode=' + conn.execute('PRAGMA journal_mode').fetchone()[0]); conn.close()"
    return Add-RssBotCommandReport -Lines $Lines -Title $Title -Command @(
        "docker", "compose", "run", "--rm", "--no-deps", "rssbot", "python", "-c", $python
    )
}

function Get-RssBotTaskNames {
    return @(
        "RSS Bot Daily Health Check",
        "RSS Bot Weekly Maintenance",
        "RSS Bot Post Reboot Check"
    )
}

function Get-RssBotStartupShortcutPath {
    $startup = [Environment]::GetFolderPath("Startup")
    return Join-Path $startup "RSS Bot Post Reboot Check.lnk"
}

Export-ModuleMember -Function *
