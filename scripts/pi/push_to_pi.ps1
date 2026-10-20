<#
.SYNOPSIS
Moves Jarvis's brain to the Pi: copies his data from this PC to the Pi (run on the PC).

.DESCRIPTION
Copies .env, jarvis_memory\ (memories, agenda, conversations, check-ins, notification topic),
workspaces.json, stage_state.json and coding_state.json into the Pi's jarvis folder.
- Jarvis must be stopped on the PC first, so nothing changes mid-copy. Until the PC becomes a
  client of the Pi (Mobile Jarvis Phase 3), run only ONE Jarvis at a time - the other one's
  memory would drift apart. scripts\pi\pull_from_pi.ps1 moves him back.
- If Jarvis is running on the Pi, he's stopped for the copy and started again afterwards.
- The Pi's previous copy is backed up to ~/jarvis_backups/<time>/ on the Pi first.
- Files are merged in (not mirrored): anything only on the Pi, like the power switch's key
  (jarvis_memory/control.json), stays.
- Voice recordings (jarvis_memory\voice\samples) stay on the PC: voice ID doesn't run on the Pi.
- Windows paths in the state files are translated on the Pi (config.PC_PATHS).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\pi\push_to_pi.ps1 danil@jarvis-pi.local
.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\pi\push_to_pi.ps1 danil@jarvis-pi.local -IncludeOutput
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$PiHost,   # user@hostname, as for ssh
    [string]$PiDir = "~/jarvis",
    [switch]$IncludeOutput,          # also jarvis_output\ (generated files the Stage may show)
    [switch]$IncludeVoiceSamples,
    [switch]$Force                   # skip the "Jarvis is running on this PC" check
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$utf8 = New-Object System.Text.UTF8Encoding $false

function Test-Port($port) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $port); $c.Close(); return $true }
    catch { return $false }
}

if ((Test-Port 5151) -and -not $Force) {
    Write-Host "Jarvis is running on this PC. Shut him down first ('Jarvis, shut down' or close his window),"
    Write-Host "so nothing changes during the copy. (-Force skips this check.)"
    exit 1
}

$items = @(".env", "jarvis_memory", "workspaces.json", "stage_state.json", "coding_state.json")
if ($IncludeOutput) { $items += "jarvis_output" }
$present = @($items | Where-Object { Test-Path (Join-Path $root $_) })
if ($present -notcontains ".env") { Write-Host "No .env in $root - nothing to copy."; exit 1 }

$exclude = @("--exclude=*.tmp")
if (-not $IncludeVoiceSamples) { $exclude += "--exclude=jarvis_memory/voice/samples" }

Write-Host "Copying to ${PiHost}:${PiDir}"
foreach ($i in $present) {
    $p = Join-Path $root $i
    $bytes = if ((Get-Item $p).PSIsContainer) {
        (Get-ChildItem $p -Recurse -File | Where-Object {
            $IncludeVoiceSamples -or $_.FullName -notlike "*\jarvis_memory\voice\samples\*" } |
            Measure-Object Length -Sum).Sum
    } else { (Get-Item $p).Length }
    Write-Host ("  {0,-20} {1,8:N1} MB" -f $i, ($bytes / 1MB))
}

$tmp = Join-Path $env:TEMP "jarvis_push"
New-Item -ItemType Directory -Force $tmp | Out-Null
$tgz = Join-Path $tmp "jarvis_push.tgz"
& tar -czf $tgz -C $root @exclude @present
if ($LASTEXITCODE -ne 0) { Write-Host "tar failed"; exit 1 }

# The remote steps go over as a script file: no quoting trouble between PowerShell, ssh and bash
$remote = @"
set -e
cd $PiDir
WAS=`$(systemctl is-active jarvis.service 2>/dev/null || true)
if [ "`$WAS" = active ]; then
    echo "Stopping Jarvis on the Pi for the copy..."
    python3 scripts/pi/control.py stop || sudo systemctl stop jarvis.service
fi
B=~/jarvis_backups/`$(date +%Y%m%d-%H%M%S)
mkdir -p "`$B"
for x in .env jarvis_memory workspaces.json stage_state.json coding_state.json jarvis_output; do
    [ -e "`$x" ] && cp -a "`$x" "`$B/"
done
tar -xzf /tmp/jarvis_push.tgz -C .
chmod 600 .env
rm -f /tmp/jarvis_push.tgz /tmp/jarvis_push.sh
echo "Copied. The Pi's previous copy is backed up in `$B"
if [ "`$WAS" = active ]; then
    echo "Starting Jarvis on the Pi again..."
    python3 scripts/pi/control.py start || sudo systemctl start jarvis.service
fi
"@
$sh = Join-Path $tmp "jarvis_push.sh"
[IO.File]::WriteAllText($sh, $remote.Replace("`r`n", "`n"), $utf8)

& scp -q $tgz $sh "${PiHost}:/tmp/"
if ($LASTEXITCODE -ne 0) { Write-Host "scp failed - can you 'ssh $PiHost'?"; exit 1 }
& ssh -t $PiHost "bash /tmp/jarvis_push.sh"
$code = $LASTEXITCODE
Remove-Item -Recurse -Force $tmp
if ($code -ne 0) { Write-Host "The copy on the Pi failed (see above)."; exit 1 }

Write-Host ""
Write-Host "Done. The Pi now has Jarvis's memory. Keep Jarvis off on this PC while the Pi is the brain;"
Write-Host "scripts\pi\pull_from_pi.ps1 $PiHost brings his memory back here."
