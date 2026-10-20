# Jarvis's brain on the Pi 5

Jarvis runs on the Pi at home; you talk to him from the `/phone` page (and the dashboard) over
Tailscale. A power page with Start / Stop buttons decides when he runs, he's stopped every night
at 11 PM in case you forget, and a heat guard stops him if the Pi ever runs hot.

Until the PC becomes a client of the Pi (Mobile Jarvis Phase 3), run **one Jarvis at a time**:
either the PC or the Pi. `push_to_pi.ps1` / `pull_from_pi.ps1` move his memory between them.

## Setup day

On the Pi (SSH from PowerShell: `ssh <user>@<pi>.local`):

```bash
cd ~/jarvis && git pull
timedatectl                                  # is the time zone yours? 11 PM uses it
sudo timedatectl set-timezone Pacific/Honolulu   # only if it isn't (pick yours)
bash scripts/pi/setup_pi.sh                  # packages, .venv, Whisper, tools for the tests (~15 min)
curl -fsSL https://claude.ai/install.sh | bash && claude   # Claude Code: sign in once, then /exit
```

Tailscale should already be up from the AirPods bench (`tailscale status`). If not:
`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`, then rerun `setup_pi.sh`
(it lets Jarvis fetch his HTTPS certificate).

**Health checks before Jarvis moves in** (results in `scripts/pi/results/`):

```bash
./scripts/pi/diag check            # heat, fan, power supply, memory, storage
./scripts/pi/diag sd               # your SD card: speed, small-save latency, errors (~3 min)
./scripts/pi/diag stress           # 15 min with all 4 cores flat out: does the cooling keep up?
```

**Move Jarvis over.** On the PC, shut Jarvis down, then in PowerShell from the jarvis folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\pi\push_to_pi.ps1 <user>@<pi>.local
```

Back on the Pi, try him in the foreground first. Open the phone link he prints, ask something,
then Ctrl+C:

```bash
.venv/bin/python jarvis.py
```

Then make it permanent: the service, the power page and the 11 PM timer:

```bash
bash scripts/pi/install_service.sh
```

It prints your **power page link** (with its key). Open it once on your phone, then use
"Add to Home Screen". The `/phone` page shows a **⏻ POWER** link to it too.

**Under real use:**

```bash
./scripts/pi/diag load                                   # 10 spoken commands: speed, CPU, heat, memory
tmux new -s soak './scripts/pi/diag soak --hours 24'      # Ctrl+B then D to leave it running
./scripts/pi/diag report && cat scripts/pi/results/report.md   # paste this to Claude
```

`tmux attach -t soak` checks on the soak test; Ctrl+C there stops it early and still summarizes.

## The power page (port 5152)

- **Start / Stop**, plus temperature, fan, free memory, what Jarvis uses, any warnings.
- It stays up while Jarvis is off (Jarvis's own `/phone` page can't), and it uses almost nothing.
- After a reboot or power cut, Jarvis comes back only if you'd left him on.
- **11 PM**: if he's running, he's stopped and your phone gets a notification with a
  **Start Jarvis** button. It's the same ntfy topic as his reminders. To change the time, edit
  `OnCalendar` in `jarvis-nightly.timer`, then rerun `install_service.sh`. A Claude Code run still
  going at 11 PM is stopped too.
- **Heat guard**: 80 °C or more for 3 minutes stops him and notifies you (`HOT_C`, `HOT_MINUTES`
  in `control.py`). The Pi slows itself down near 85 °C anyway; this is a backstop.
- From SSH: `python3 scripts/pi/control.py start|stop|status|link`.

## The health checks (`./scripts/pi/diag`)

| Step | What it does | Good result |
|---|---|---|
| `check` | Temperature, fan, throttling/under-voltage since boot, input voltage, what the power supply offers, memory, storage, file-system errors, services, Tailscale | All OK. A non-official supply shows "3 A": fine for Jarvis, USB ports limited to 600 mA |
| `sd` | Bus speed, big reads/writes, small random reads/writes (A1/A2 class), **small saves with fsync** (what Jarvis does), kernel SD errors. Writes a ~1 GB test file, then deletes it | A1 or better, saves under ~50 ms, no errors |
| `stress` | `stress-ng` on 4 cores for 15 min (`--minutes`), temperature/speed/fan every 2 s, stops at 85 °C | Peak under 75 °C, no slowdown |
| `load` | Speaks 10 commands (`--rounds`) with espeak-ng and sends them to `/phone-command` like your phone (Whisper included). Costs a few API tokens; they show up in his history | Replies in a few seconds, CPU and heat modest |
| `soak` | Every minute for 24 h (`--hours`): temperature, throttling, CPU, memory, Jarvis's state/memory/restarts, SD writes | Steady memory (no growth per hour), no restarts, writes per day → rough SD wear estimate |
| `report` | Everything above in `results/report.md` | |
| `selftest` | Offline check of the parsers, summaries and the power switch (any OS) | all PASS |

## Moving Jarvis back to the PC

Run this on the PC: it stops Jarvis on the Pi and brings his memory back (the PC's copy is
backed up to `%USERPROFILE%\jarvis_backups\` first).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\pi\pull_from_pi.ps1 <user>@<pi>.local
```

## Files

| File | |
|---|---|
| `setup_pi.sh` | Packages (incl. stress-ng, fio, espeak-ng), `.venv`, Whisper and speaker models, Tailscale cert permission |
| `install_service.sh` | `jarvis.service`, `jarvis-control.service`, `jarvis-nightly.timer`, the sudoers rule; `remove` undoes all |
| `control.py` | The power switch: page, Start/Stop, heat guard, 11 PM stop, boot state (`jarvis_memory/control.json`) |
| `diagnose.py`, `diag`, `pihealth.py` | Health checks; `pihealth.py` has the readings shared with `control.py` |
| `push_to_pi.ps1`, `pull_from_pi.ps1` | Move Jarvis's data PC → Pi and back (run on the PC) |
| `diag_selftest.py` | Offline tests for all of the above |
