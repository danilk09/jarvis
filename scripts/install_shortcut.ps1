# Adds a "Jarvis" shortcut to the Desktop and the Start menu (pin it to the taskbar from there).
# Clicking it starts Jarvis hidden with its window open, or brings the window up if Jarvis is
# already running. Closing the window shuts Jarvis down.
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
# Undo: delete Jarvis.lnk from the Desktop and from the Start menu's Programs folder.

$ErrorActionPreference = "Stop"
$root    = Split-Path -Parent $PSScriptRoot
$python  = (Get-Command python).Source
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"   # no console window
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }
$icon = Join-Path $root "desktop\icon.ico"
if (-not (Test-Path $icon)) { & $python (Join-Path $root "scripts\make_icon.py") }

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "Jarvis.lnk"))
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = "`"$root\scripts\jarvis_launcher.pyw`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation     = "$icon,0"
    $lnk.Description      = "Start JARVIS (or show its window)"
    $lnk.Save()
    Write-Host "Shortcut: $dir\Jarvis.lnk"
}
