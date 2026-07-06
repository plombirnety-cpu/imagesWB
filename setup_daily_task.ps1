# setup_daily_task.ps1 - registers a daily run of daily_prints.py in Windows Task
# Scheduler.
#
# Usage (PowerShell, run as the user with schtasks rights):
#   .\setup_daily_task.ps1                       # default time 07:00
#   .\setup_daily_task.ps1 -Time 06:30           # custom time
#   .\setup_daily_task.ps1 -TaskName "MyTask"    # custom task name
#
# Remove the task:
#   schtasks /delete /tn "PrintFactoryNB-Daily" /f

param(
    [string]$Time = "07:00",
    [string]$TaskName = "PrintFactoryNB-Daily"
)

$ProjectDir = $PSScriptRoot
$LogDir = Join-Path $ProjectDir "out_batch"
$LogFile = Join-Path $LogDir "daily.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    Write-Error "python not found in PATH - install Python or fix PythonExe in this script."
    exit 1
}

# schtasks runs the command through cmd.exe - wrap in cmd /c with log redirect.
# /RL LIMITED - regular user rights (image generation does not need admin rights).
$Command = "cmd.exe"
$Arguments = "/c cd /d `"$ProjectDir`" && `"$PythonExe`" daily_prints.py >> `"$LogFile`" 2>&1"

Write-Host "Registering task '$TaskName': daily at $Time" -ForegroundColor Cyan
Write-Host "Working directory: $ProjectDir"
Write-Host "Log file: $LogFile"

schtasks /create `
    /tn "$TaskName" `
    /tr "$Command $Arguments" `
    /sc DAILY `
    /st $Time `
    /rl LIMITED `
    /f

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Done. Check with: schtasks /query /tn `"$TaskName`" /v /fo LIST" -ForegroundColor Green
    Write-Host "Run now (test): schtasks /run /tn `"$TaskName`""
    Write-Host "Remove task: schtasks /delete /tn `"$TaskName`" /f"
} else {
    Write-Error "schtasks failed (exit code $LASTEXITCODE) - see output above."
}
