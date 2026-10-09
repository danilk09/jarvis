# Removes the JARVIS login task created by install_startup.ps1 (and stops it if running).
Stop-ScheduledTask -TaskName "JARVIS" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "JARVIS" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "JARVIS login task removed."
