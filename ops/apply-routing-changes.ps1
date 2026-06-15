param(
    [switch]$Build
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host ""
    Write-Host "== $Name =="
    & $Command
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Invoke-Step "Validate bot config and env" {
    python -m app.main --validate-config --validate-env
}

Invoke-Step "Validate routing config" {
    python -m app.main --validate-routing
}

Invoke-Step "Routing diagnostics" {
    python -m app.main --routing-diagnostics
}

$routeTestsPath = Join-Path $root "config\routing\route_tests.json"
if (Test-Path $routeTestsPath) {
    Invoke-Step "Saved route tests" {
        $tests = Get-Content $routeTestsPath -Raw | ConvertFrom-Json
        foreach ($test in $tests) {
            if ($test -is [string]) {
                python -m app.main --route-test-title $test
            } else {
                $arguments = @("--route-test-title", [string]$test.title)
                if ($test.summary) { $arguments += @("--route-test-summary", [string]$test.summary) }
                if ($test.source) { $arguments += @("--route-test-source", [string]$test.source) }
                if ($test.source_id) { $arguments += @("--route-test-source-id", [string]$test.source_id) }
                if ($test.source_class) { $arguments += @("--route-test-source-class", [string]$test.source_class) }
                if ($test.url) { $arguments += @("--route-test-url", [string]$test.url) }
                python -m app.main @arguments
            }
        }
    }
}

if ($Build) {
    Invoke-Step "Build rssbot image" {
        docker compose build rssbot
    }
}

Invoke-Step "Recreate rssbot container" {
    docker compose up -d --force-recreate rssbot
}

Invoke-Step "Compose status" {
    docker compose ps
}

Invoke-Step "Recent health logs" {
    docker compose logs --since 5m rssbot |
        Select-String -Pattern "Connected to Discord|Routing config loaded|Configured|heartbeat blocked|disk I/O error|database disk image"
}
