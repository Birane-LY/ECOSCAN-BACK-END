$ErrorActionPreference = "Stop"

$backendDirectory = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $backendDirectory "docker-compose.energy-jobs.yml"
$envFile = Join-Path $backendDirectory ".env"
$logDirectory = Join-Path $env:LOCALAPPDATA "EcoScan\logs"
$logFile = Join-Path $logDirectory "daily-energy-jobs.log"

if (-not (Test-Path $envFile -PathType Leaf)) {
    throw "Backend .env is missing: $envFile. Copy .env.example to .env and configure it first."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is unavailable to this Windows account."
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Start-Transcript -Path $logFile -Append | Out-Null

try {
    Push-Location $backendDirectory
    try {
        & docker compose `
            --project-directory $backendDirectory `
            --file $composeFile `
            run --rm --no-deps energy-jobs `
            python manage.py executer_taches_energetiques_quotidiennes
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw "Daily energy jobs failed with exit code $exitCode. See $logFile."
    }
}
finally {
    Stop-Transcript | Out-Null
}
