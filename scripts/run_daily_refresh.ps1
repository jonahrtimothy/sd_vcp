# Windows Task Scheduler entrypoint: runs the evening data refresh + scan.
# Registered by Claude Code on 2026-08-31 -- see SYSTEM_BUILD_PROMPT.md
# Section 13. Requires that day's Kite login/exchange to have been done
# already (the access_token is still valid at evening trading-close time,
# but is NOT auto-generated here -- that stays a manual morning step by
# design, see kite_auth.py).

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root "venv\Scripts\python.exe"
$logDir = Join-Path $root "data"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "task_scheduler.log"

"=== Run started: $(Get-Date -Format o) ===" | Out-File -FilePath $logFile -Append -Encoding utf8

& $py "scripts\refresh_data.py" *>> $logFile
& $py "scripts\run_scan.py" *>> $logFile

"=== Run finished: $(Get-Date -Format o) ===" | Out-File -FilePath $logFile -Append -Encoding utf8
