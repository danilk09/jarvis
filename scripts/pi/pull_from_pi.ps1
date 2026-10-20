<#
.SYNOPSIS
Moves Jarvis's brain back to this PC: copies his memory from the Pi (run on the PC).

.DESCRIPTION
Stops Jarvis on the Pi (so the two never run at once) and copies jarvis_memory/ back:
memories, agenda, conversations, daily summaries, check-ins, chat history.
- stage_state.json / coding_state.json stay where they are: they hold the Pi's own paths, and
  Claude Code sessions only resume on the machine that ran them.
- This PC's jarvis_memory is backed up to %USERPROFILE%\jarvis_backups\<time>\ first, and the
  Pi's copy is merged in (your voice recordings here are kept).
- Jarvis on the Pi is left stopped; start him on the PC as usual.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\pi\pull_from_pi.ps1 danil@jarvis-pi.local
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$PiHost,
    [string]$PiDir = "~/jarvis"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$utf8 = New-Object System.Text.UTF8Encoding $false

function Test-Port($port) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $port); $c.Close(); return $true }
    catch { return $false }
}
if (Test-Port 5151) {
    Write-Host "Jarvis is running on this PC - shut him down first, then pull."
    exit 1
}

$tmp = Join-Path $env:TEMP "jarvis_pull"
New-Item -ItemType Directory -Force $tmp | Out-Null
$remote = @"
set -e
cd $PiDir
if [ "`$(systemctl is-active jarvis.service 2>/dev/null || true)" = active ]; then
    echo "Stopping Jarvis on the Pi..."
    python3 scripts/pi/control.py stop || sudo systemctl stop jarvis.service
fi
tar -czf /tmp/jarvis_pull.tgz --exclude=jarvis_memory/control.json --exclude='*.tmp' jarvis_memory
rm -f /tmp/jarvis_pull.sh
"@
$sh = Join-Path $tmp "jarvis_pull.sh"
[IO.File]::WriteAllText($sh, $remote.Replace("`r`n", "`n"), $utf8)
& scp -q $sh "${PiHost}:/tmp/"
if ($LASTEXITCODE -ne 0) { Write-Host "scp failed - can you 'ssh $PiHost'?"; exit 1 }
& ssh -t $PiHost "bash /tmp/jarvis_pull.sh"
if ($LASTEXITCODE -ne 0) { Write-Host "Packing on the Pi failed (see above)."; exit 1 }
$tgz = Join-Path $tmp "jarvis_pull.tgz"
& scp -q "${PiHost}:/tmp/jarvis_pull.tgz" $tgz
if ($LASTEXITCODE -ne 0) { Write-Host "Download failed."; exit 1 }
& ssh $PiHost "rm -f /tmp/jarvis_pull.tgz"

$mem = Join-Path $root "jarvis_memory"
if (Test-Path $mem) {
    $backup = Join-Path $env:USERPROFILE ("jarvis_backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    New-Item -ItemType Directory -Force $backup | Out-Null
    Copy-Item -Recurse $mem $backup
    Write-Host "This PC's previous memory is backed up in $backup"
}
& tar -xzf $tgz -C $root
if ($LASTEXITCODE -ne 0) { Write-Host "Unpacking failed - your memory is untouched or in the backup above."; exit 1 }
Remove-Item -Recurse -Force $tmp
Write-Host "Done: Jarvis's memory is back on this PC, and he's stopped on the Pi. Start him here as usual."
