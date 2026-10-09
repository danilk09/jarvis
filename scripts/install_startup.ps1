# Registers JARVIS to start hidden in background mode when you log in.
# NOT run automatically — run it yourself once JARVIS is ready for 24/7 use:
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
# Undo with scripts\uninstall_startup.ps1

$ErrorActionPreference = "Stop"
$root    = Split-Path -Parent $PSScriptRoot
$python  = (Get-Command python).Source
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"   # no console window
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$root\jarvis.py`" --background" -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "JARVIS" -Action $action -Trigger $trigger -Settings $settings `
    -Description "JARVIS voice assistant (background mode)" -Force | Out-Null
Write-Host "JARVIS will start at login. Logs: $root\logs\jarvis.log"
Write-Host "Start it now with:  Start-ScheduledTask -TaskName JARVIS"
