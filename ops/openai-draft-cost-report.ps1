param(
    [string]$Path = "",
    [int]$Last = 0
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $Path) {
    $Path = Join-Path $repoRoot "logs\openai-draft-metrics.jsonl"
}
if (-not (Test-Path -LiteralPath $Path)) {
    throw "Metrics file not found: $Path"
}

$rows = Get-Content -LiteralPath $Path |
    Where-Object { $_.Trim() } |
    ForEach-Object { $_ | ConvertFrom-Json }
if ($Last -gt 0) {
    $rows = @($rows | Select-Object -Last $Last)
}

function Get-Percentile {
    param([double[]]$Values, [double]$Percentile)
    if (-not $Values -or $Values.Count -eq 0) {
        return 0
    }
    $sorted = @($Values | Sort-Object)
    $index = [math]::Ceiling(($Percentile / 100) * $sorted.Count) - 1
    $index = [math]::Max(0, [math]::Min($index, $sorted.Count - 1))
    return [double]$sorted[$index]
}

$groups = @($rows | Group-Object profile)
foreach ($group in $groups) {
    $items = @($group.Group)
    $success = @($items | Where-Object status -eq "ok")
    $latencies = [double[]]@($success | ForEach-Object { [double]$_.elapsed_ms })
    $billable = @($items | Where-Object { $null -ne $_.estimated_cost_usd })
    $totalCost = ($billable | Measure-Object -Property estimated_cost_usd -Sum).Sum
    if ($null -eq $totalCost) {
        $totalCost = 0
    }
    $averageCost = if ($success.Count) { [double]$totalCost / $success.Count } else { 0 }
    [pscustomobject]@{
        Profile = $group.Name
        Requests = $items.Count
        Successes = $success.Count
        ErrorRatePercent = [math]::Round((($items.Count - $success.Count) / [math]::Max(1, $items.Count)) * 100, 2)
        P50Seconds = [math]::Round((Get-Percentile -Values $latencies -Percentile 50) / 1000, 2)
        P95Seconds = [math]::Round((Get-Percentile -Values $latencies -Percentile 95) / 1000, 2)
        SearchCalls = ($billable | Measure-Object -Property web_search_calls -Sum).Sum
        InputTokens = ($billable | Measure-Object -Property input_tokens -Sum).Sum
        OutputTokens = ($billable | Measure-Object -Property output_tokens -Sum).Sum
        TotalEstimatedCostUsd = [math]::Round([double]$totalCost, 4)
        AverageCostUsd = [math]::Round($averageCost, 4)
    }
}
