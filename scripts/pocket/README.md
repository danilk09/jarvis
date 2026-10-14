# Pocket device: AirPods bench test

Before buying more parts, this proves whether a Raspberry Pi can use **AirPods Pro as Jarvis's
mic, speaker and button**. It runs on the Pi 5 now (the Zero 2 W is sold out) and on the
Zero 2 W later, with the same commands.

It answers four questions:

1. Does music mode (A2DP) play from the Pi to the AirPods?
2. Which stem presses reach Linux? Press-and-hold (preferred trigger), double-press (fallback)?
3. Does the AirPods mic work in headset mode (HFP), and how clear is it (mSBC 16 kHz vs CVSD 8 kHz)?
4. End to end: press, speak, PC Jarvis answers, reply plays in your ears. How fast?

## What you need

- The Pi 5 with Raspberry Pi OS (64-bit, Lite is fine), on Wi-Fi, reachable over SSH
- Your AirPods Pro, charged
- For step 6 only: Jarvis running on the PC, Tailscale on both the PC and the Pi

## 1. Get the code onto the Pi (once)

```bash
sudo apt update && sudo apt install -y git gh
gh auth login                      # GitHub.com -> HTTPS -> log in with a browser
gh repo clone danilk09/jarvis ~/jarvis -- -b pi_brain
cd ~/jarvis
./scripts/pocket/bench selftest    # optional: offline check of the bench itself, needs no setup
```

Already cloned? `cd ~/jarvis && git fetch && git checkout pi_brain && git pull`.

## 2. Set up (once, ~10-15 min)

```bash
bash scripts/pocket/setup_bench.sh
sudo reboot
```

It installs Bluetooth + PipeWire audio, sets WirePlumber up for headset mode on a headless
Pi, installs the **SCO routing fix** (the Pi's Bluetooth chip sends headset-mic audio to
unused pins by default, so the mic would record silence), and downloads Whisper and
Jarvis's Piper voice.

## 3. Set the AirPods button (on the iPhone)

Settings -> your AirPods Pro -> Press and Hold: set **one earbud to Siri** (that one becomes
the Jarvis button). Note the model shown there (1st or 2nd generation).

During the test keep the iPhone's Bluetooth **off** (or the phone far away), or it will grab
the AirPods back.

## 4. Run the test (~15 min)

```bash
cd ~/jarvis
./scripts/pocket/bench all
```

It walks you through each step and waits for you:

| Step | What you do | What it measures |
|---|---|---|
| check | nothing | Bluetooth, PipeWire, the WirePlumber config, SCO fix, groups |
| pair | case lid open, hold the case button until white (if the AirPods show up without their name, pick them from the list) | pairing, which audio profiles appear (waits ~12 s for the headset profile) |
| sound | listen | chime + sentence in music mode: heard or not |
| buttons | hold / double / single / triple press, 6 s each | which events reach the Pi (HFP AT commands via `btmon`, AVRCP keys via evdev; a silent stream plays meanwhile so the AirPods send presses); also the HFP handshake |
| mic | say the sentence after each chime, then rate the playback 1-5 | mSBC and CVSD: level, mode-switch time, Whisper transcript, word error |
| jarvis (optional) | press your trigger, give real commands, Ctrl+C to stop | full round trip to the PC Jarvis and its latency |

Steps can be run on their own: `./scripts/pocket/bench mic`, `./scripts/pocket/bench jarvis --jarvis https://your-pc.tail1234.ts.net:5151`.
`./scripts/pocket/bench report` prints the summary again; `./scripts/pocket/bench reset` clears results (keeps the pairing).
`./scripts/pocket/bench selftest` runs the parsers, profile picking, word error rate, WAV writing and the
verdict on sample `pactl`/`btmon` output (`bench_selftest.py`), no Bluetooth needed.

## 5. Read the verdict

Results go to `scripts/pocket/results/` (git-ignored):

- `report.md`: **paste this to Claude.** Verdict, table of every test, raw numbers
- `mic_msbc.wav`, `mic_cvsd.wav`: your recordings
- `btmon_buttons.log`: the full Bluetooth capture of the button test (useful if a press isn't recognized)

| Verdict | Meaning | Next |
|---|---|---|
| **GO** | Mic works, press-and-hold arrives | Plan stands. Buy the Zero 2 W when it's back, rerun this bench on it |
| **GO (double-press trigger)** | Mic works, only double-press arrives | Use double-press; "next song" becomes a voice command. Hold may be fixable later |
| **GO (needs another trigger)** | Mic works, no press arrives | Button on the device, a BLE remote, or a wake word on the device |
| **NO-GO (AirPods mic)** | Music works, mic doesn't | Send the report first (silence = SCO routing, may be fixable). Else: mic on the device, AirPods for listening only |
| **LIKELY GO** | Mic works, buttons step not run | `./scripts/pocket/bench buttons` |
| **INCOMPLETE** | Pairing, music or the mic step not done yet | Run the missing steps |

A mic run counts as working at ≤35% word error or a 3+/5 rating (rating only, if Whisper isn't installed).

## Notes

- The Pi 5 and Zero 2 W have different Bluetooth chips (CYW43455 vs CYW43438). A pass here is a
  strong sign, not a guarantee; rerun on the Zero.
- Step 6 sends the recording to the PC's existing `/phone-command` endpoint and reads the reply
  from `/status`, like the phone page does, then speaks it on the Pi with Piper. The PC may
  speak the reply too.
- AirPods connect to one non-Apple device at a time. When you're done:
  `bluetoothctl disconnect <MAC>` and turn the iPhone's Bluetooth back on.
- Nothing here touches the main Jarvis install; Phase 2 (moving the brain) is separate.

## Troubleshooting

| Problem | Try |
|---|---|
| `check` shows WirePlumber/PipeWire not running | `systemctl --user restart pipewire pipewire-pulse wireplumber` |
| No `headset-head-unit` profile after pairing | Reboot, then `bluetoothctl disconnect <MAC>` and `./scripts/pocket/bench pair` |
| Mic recording is silent | `sudo systemctl restart jarvis-bt-sco` then `./scripts/pocket/bench mic`; send the report |
| AirPods keep jumping back to the iPhone | iPhone Bluetooth off during the test |
| Button step sees nothing at all | Make sure the AirPods are in your ears; check `results/btmon_buttons.log` |
| `bench jarvis` can't reach the PC | Jarvis running? `tailscale status` on both; use the address Jarvis prints, without `/phone` |
