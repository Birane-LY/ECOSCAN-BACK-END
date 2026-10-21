param(
    [ValidatePattern('^\d{2}:\d{2}$')]
    [string]$StartTime = "02:15"
)

$ErrorActionPreference = "Stop"

$backendDirectory = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run-daily-energy-jobs.ps1"
$envFile = Join-Path $backendDirectory ".env"
$taskName = "EcoScan V2 - Daily energy jobs"

if (-not (Test-Path $envFile -PathType Leaf)) {
    throw "Backend .env is missing: $envFile. Copy .env.example to .env and configure it first."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is unavailable to this Windows account."
}

$parts = $StartTime.Split(":")
$hour = [int]$parts[0]
$minute = [int]$parts[1]
if ($hour -gt 23 -or $minute -gt 59) {
    throw "StartTime must be a valid 24-hour time in HH:mm format."
}

$time = [datetime]::Today.AddHours($hour).AddMinutes($minute)
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Runs EcoScan V2 daily telemetry, energy alert, invoice comparison, and objective progression checks." `
    -Force | Out-Null

Write-Output "Registered '$taskName' daily at $StartTime."
Write-Output "The task runs only while this Windows account is logged in and Docker Desktop is available."
Write-Output "The first run will fail until Docker Desktop is started and the backend .env/database are configured."
Write-Output "Logs: $env:LOCALAPPDATA\EcoScan\logs\daily-energy-jobs.log"
