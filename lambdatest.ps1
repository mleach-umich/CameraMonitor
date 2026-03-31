$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

docker compose exec -T webcam-monitor python /app/webcam_monitor.py --lambdatest
