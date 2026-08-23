[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,31}$')]
    [string]$AccountCode,

    # Creates this profile by copying the currently selected profile. Use only
    # for naming/preserving a known-good existing ChatGPT Codex session.
    [switch]$CloneCurrent,

    # Starts Codex's official device-login flow in the selected profile.
    # Use this for a new account, not to work around a usage-limit message.
    [switch]$Authenticate
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing $envFile. Create it from .env.example first."
}

$profileVolume = "codex-auth-$AccountCode"
$existingSetting = (Get-Content -LiteralPath $envFile | Where-Object {
    $_ -match '^CODEX_AUTH_VOLUME='
} | Select-Object -First 1) -replace '^CODEX_AUTH_VOLUME=', ''

if (-not $existingSetting) {
    throw 'CODEX_AUTH_VOLUME is not set in .env.'
}

& docker volume inspect $profileVolume 2>$null | Out-Null
$profileExists = $LASTEXITCODE -eq 0
if (-not $profileExists) {
    & docker volume create $profileVolume | Out-Null
    if ($CloneCurrent) {
        & docker run --rm `
            -v "${existingSetting}:/from:ro" `
            -v "${profileVolume}:/to" `
            alpine:3.21 sh -c 'cp -a /from/. /to/'
        if ($LASTEXITCODE -ne 0) { throw "Could not copy the current Codex profile into $profileVolume." }
    }
    else {
        Write-Host "Created empty profile $profileVolume. Authenticate it before dispatching workers."
    }
}

$content = Get-Content -LiteralPath $envFile -Raw
if ($content -match '(?m)^CODEX_AUTH_VOLUME=.*$') {
    $content = [regex]::Replace($content, '(?m)^CODEX_AUTH_VOLUME=.*$', "CODEX_AUTH_VOLUME=$profileVolume")
}
else {
    $content = $content.TrimEnd("`r", "`n") + "`r`nCODEX_AUTH_VOLUME=$profileVolume`r`n"
}
[System.IO.File]::WriteAllText($envFile, $content, [System.Text.UTF8Encoding]::new($false))

Push-Location $projectRoot
try {
    & docker compose up -d --force-recreate factory-executor
    if ($LASTEXITCODE -ne 0) { throw 'Factory executor recreation failed.' }
}
finally {
    Pop-Location
}

Write-Host "Active Codex profile: $profileVolume"
Write-Host "Verify with: docker run --rm --user 10002:10002 -v ${profileVolume}:/home/worker/.codex --entrypoint codex hermes-codex-worker:local login status"

if ($Authenticate) {
    Write-Host "Starting device login for $profileVolume. Complete the displayed code only in the browser window you open yourself."
    & docker run --rm --user root `
        -v "${profileVolume}:/home/worker/.codex" `
        --entrypoint sh hermes-codex-worker:local `
        -c 'mkdir -p /home/worker/.codex && chown -R 10002:10002 /home/worker/.codex'
    if ($LASTEXITCODE -ne 0) { throw "Could not initialize Codex profile $profileVolume." }

    & docker run --rm -it --user 10002:10002 `
        -v "${profileVolume}:/home/worker/.codex" `
        --entrypoint codex hermes-codex-worker:local `
        login --device-auth
    if ($LASTEXITCODE -ne 0) { throw "Codex device login did not complete for $profileVolume." }

    & docker run --rm --user 10002:10002 `
        -v "${profileVolume}:/home/worker/.codex" `
        --entrypoint codex hermes-codex-worker:local `
        login status
    if ($LASTEXITCODE -ne 0) { throw "Codex login verification failed for $profileVolume." }
}
