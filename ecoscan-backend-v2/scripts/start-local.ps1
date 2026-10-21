param(
    [ValidateRange(0.1, 3600)]
    [double]$SensorPollIntervalSeconds = 1
)

$ErrorActionPreference = "Stop"

$backendDirectory = Split-Path -Parent $PSScriptRoot
$repositoryDirectory = Split-Path -Parent $backendDirectory
$python = Join-Path $repositoryDirectory ".venv\Scripts\python.exe"
$logDirectory = Join-Path $env:LOCALAPPDATA "EcoScan\logs"
$backendLog = Join-Path $logDirectory "backend-server.log"
$backendErrorLog = Join-Path $logDirectory "backend-server-error.log"
$simulatorLog = Join-Path $logDirectory "sensor-simulator.log"
$simulatorErrorLog = Join-Path $logDirectory "sensor-simulator-error.log"

if (-not (Test-Path $python -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python was not found. Create the backend virtual environment first."
    }
    $python = $pythonCommand.Source
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Push-Location $backendDirectory
try {
    $listeners = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
    if ($listeners) {
        try {
            $health = Invoke-WebRequest `
                -Uri "http://127.0.0.1:8000/admin/login/" `
                -Method Get `
                -UseBasicParsing `
                -TimeoutSec 5
            if ([int]$health.StatusCode -ne 200) {
                throw "Unexpected health status $([int]$health.StatusCode)."
            }
            Write-Output "Backend already healthy on http://127.0.0.1:8000."
        }
        catch {
            throw "Port 8000 is occupied, but the EcoScan backend health check failed: $($_.Exception.Message)"
        }
    }
    else {
        $backend = Start-Process `
            -FilePath $python `
            -ArgumentList @("-u", "manage.py", "runserver", "127.0.0.1:8000", "--noreload") `
            -WorkingDirectory $backendDirectory `
            -RedirectStandardOutput $backendLog `
            -RedirectStandardError $backendErrorLog `
            -PassThru

        $backendReady = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            Start-Sleep -Seconds 1
            $backend.Refresh()
            if ($backend.HasExited) {
                throw "Backend stopped during startup. Check $backendLog."
            }
            try {
                $health = Invoke-WebRequest `
                    -Uri "http://127.0.0.1:8000/admin/login/" `
                    -Method Get `
                    -UseBasicParsing `
                    -TimeoutSec 3
                if ([int]$health.StatusCode -eq 200) {
                    $backendReady = $true
                    break
                }
            }
            catch {
                continue
            }
        }
        if (-not $backendReady) {
            throw "Backend did not become healthy. Check $backendLog."
        }
        Write-Output "Backend started on http://127.0.0.1:8000."
    }

    $simulator = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "^(python|pythonw)(\.exe)?$" -and
        $_.CommandLine -match "(?i)manage\.py['`"]?\s+simuler_capteurs"
    } | Select-Object -First 1

    if ($simulator) {
        Write-Output "Sensor simulator already running (PID $($simulator.ProcessId))."
    }
    else {
        $simulatorProcess = Start-Process `
            -FilePath $python `
            -ArgumentList @(
                "-u",
                "manage.py",
                "simuler_capteurs",
                "--interval",
                "$SensorPollIntervalSeconds"
            ) `
            -WorkingDirectory $backendDirectory `
            -RedirectStandardOutput $simulatorLog `
            -RedirectStandardError $simulatorErrorLog `
            -PassThru

        Start-Sleep -Seconds 3
        $simulatorProcess.Refresh()
        if ($simulatorProcess.HasExited) {
            throw "Sensor simulator stopped during startup. Check $simulatorLog."
        }
        Write-Output "Sensor simulator started (PID $($simulatorProcess.Id)); it checks for due simulated sensor readings every $SensorPollIntervalSeconds second(s)."
    }

    Write-Output "Simulator log: $simulatorLog"
}
finally {
    Pop-Location
}
