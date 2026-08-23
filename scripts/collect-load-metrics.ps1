param(
    [ValidateRange(1, 168)]
    [int]$SinceHours = 24
)

$ErrorActionPreference = "Stop"
$since = "${SinceHours}h"

$factoryMetrics = docker compose exec -T factory-executor python -c @'
import json, os, urllib.request
request = urllib.request.Request(
    "http://localhost:8080/v1/metrics",
    headers={"X-Factory-Key": os.environ["FACTORY_API_KEY"]},
)
print(json.dumps(json.load(urllib.request.urlopen(request))))
'@ | ConvertFrom-Json

$hermesLogs = docker compose logs --no-color --since $since hermes hermes-gateway 2>&1 | Out-String
$sqliteLockCount = ([regex]::Matches($hermesLogs, "database is locked|SQLITE_BUSY|Resource busy")).Count
$walFallbackCount = ([regex]::Matches($hermesLogs, "WAL-reset corruption bug")).Count
$usageMatches = [regex]::Matches($hermesLogs, '"total"\s*:\s*(\d+)')
$lastReportedTokens = if ($usageMatches.Count) { [int64]$usageMatches[$usageMatches.Count - 1].Groups[1].Value } else { $null }

$resourceRows = @(docker stats --no-stream --format '{{json .}}' | ForEach-Object {
    $_ | ConvertFrom-Json
} | Where-Object {
    $_.Name -like "hermes-codex-factory-*"
})

[ordered]@{
    collected_at = (Get-Date).ToUniversalTime().ToString("o")
    window = $since
    factory = $factoryMetrics
    hermes = [ordered]@{
        sqlite_lock_events = $sqliteLockCount
        vulnerable_wal_fallback_warnings = $walFallbackCount
        last_reported_session_tokens = $lastReportedTokens
    }
    docker = $resourceRows
} | ConvertTo-Json -Depth 12
