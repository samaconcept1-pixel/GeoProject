$ErrorActionPreference = 'Stop'

$projectRoot = 'C:\GeoProject'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logDirectory = Join-Path $projectRoot 'logs'
$outputLog = Join-Path $logDirectory 'geoproject-startup.log'
$errorLog = Join-Path $logDirectory 'geoproject-startup-error.log'

if (-not (Test-Path $python)) {
    throw "Python virtual environment not found: $python"
}

if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    exit 0
}

for ($attempt = 0; $attempt -lt 12 -and -not (Test-Path 'Z:\'); $attempt++) {
    Start-Sleep -Seconds 5
}

if (-not (Test-Path 'Z:\')) {
    throw 'Project drive Z: is not available.'
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$env:CONFIG_PATH = 'config.local.yaml'

Start-Process `
    -FilePath $python `
    -ArgumentList '-m uvicorn main:app --host 0.0.0.0 --port 8000' `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outputLog `
    -RedirectStandardError $errorLog