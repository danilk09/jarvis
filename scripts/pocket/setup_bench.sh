#!/usr/bin/env bash
# One-time setup for the AirPods bench test on a Raspberry Pi (Pi 5 now, Zero 2 W later).
# Run from anywhere inside the cloned repo, as your normal user (it asks for sudo itself):
#
#     bash scripts/pocket/setup_bench.sh
#
# Safe to run again. Reboot when it finishes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VENV="$HERE/.venv"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo (audio runs in your user session)."
    exit 1
fi

step() { echo; echo "==> $*"; }

step "Installing system packages (Bluetooth, PipeWire audio, tools)"
sudo apt-get update
sudo apt-get install -y \
    bluez pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth \
    pulseaudio-utils alsa-utils ffmpeg espeak-ng \
    python3-venv python3-dev python3-evdev python3-numpy python3-requests \
    coreutils curl bzip2 git

# PulseAudio and PipeWire fight over Bluetooth; PipeWire wins here.
if dpkg -s pulseaudio >/dev/null 2>&1; then
    step "Removing PulseAudio (replaced by PipeWire's pulse server)"
    sudo apt-get remove -y pulseaudio pulseaudio-module-bluetooth || true
fi

step "Bluetooth on"
sudo rfkill unblock bluetooth || true
sudo systemctl enable --now bluetooth

step "Letting your user read button events and keep audio running without a login"
sudo usermod -aG input,bluetooth,audio "$USER"
sudo loginctl enable-linger "$USER"

step "Configuring WirePlumber for headset mode (HFP gateway, mSBC, no seat check)"
WP_VER="$(dpkg-query -W -f='${Version}' wireplumber 2>/dev/null | grep -oE '^[0-9]+\.[0-9]+' || true)"
[ -n "$WP_VER" ] || WP_VER="$(wireplumber --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
[ -n "$WP_VER" ] || WP_VER="0.5"
if [ "$(printf '%s\n' "$WP_VER" 0.5 | sort -V | head -1)" = "0.5" ]; then
    mkdir -p "$HOME/.config/wireplumber/wireplumber.conf.d"
    cp "$HERE/config/51-jarvis-bluetooth.conf" "$HOME/.config/wireplumber/wireplumber.conf.d/"
    echo "    WirePlumber $WP_VER: wrote ~/.config/wireplumber/wireplumber.conf.d/51-jarvis-bluetooth.conf"
else
    mkdir -p "$HOME/.config/wireplumber/bluetooth.lua.d"
    cp "$HERE/config/51-jarvis-bluetooth.lua" "$HOME/.config/wireplumber/bluetooth.lua.d/"
    echo "    WirePlumber $WP_VER: wrote ~/.config/wireplumber/bluetooth.lua.d/51-jarvis-bluetooth.lua"
fi

step "Starting PipeWire for your user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload || true
systemctl --user enable pipewire pipewire-pulse wireplumber 2>/dev/null || true
systemctl --user restart pipewire pipewire-pulse wireplumber || true

step "Installing the SCO routing fix (headset mic over HCI) as a boot service"
sudo tee /etc/systemd/system/jarvis-bt-sco.service >/dev/null <<EOF
[Unit]
Description=Jarvis: route Bluetooth SCO (headset mic) audio over HCI
# bthelper@hci0 (Raspberry Pi OS) power-cycles the adapter at boot; run after it
After=bluetooth.service bthelper@hci0.service
Requires=bluetooth.service

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/python3 $HERE/sco_route.py
RemainAfterExit=yes

[Install]
WantedBy=bluetooth.service
EOF
sudo systemctl daemon-reload
sudo systemctl enable jarvis-bt-sco.service
sudo systemctl restart jarvis-bt-sco.service || echo "    (will retry at boot)"

step "Python environment for the bench ($VENV)"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null || true
# Both are optional for the bench (accuracy scoring, Piper voice), so a failed install doesn't stop setup
"$VENV/bin/pip" install faster-whisper || echo "    faster-whisper failed to install - the mic test runs without accuracy scoring."
"$VENV/bin/pip" install sherpa-onnx || echo "    sherpa-onnx failed to install - replies use espeak-ng instead of Piper."

step "Downloading the speech-recognition model (Whisper base.en)"
"$VENV/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')" \
    || echo "    Whisper download failed - the mic test still runs, without accuracy scoring."

step "Downloading Jarvis's voice (Piper) for spoken replies"
mkdir -p "$REPO/models/tts"
VOICE="vits-piper-en_GB-northern_english_male-medium"
if [ ! -d "$REPO/models/tts/$VOICE" ]; then
    curl -fL -o "$REPO/models/tts/voice.tar.bz2" \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/$VOICE.tar.bz2" \
        && tar xjf "$REPO/models/tts/voice.tar.bz2" -C "$REPO/models/tts/" \
        && rm -f "$REPO/models/tts/voice.tar.bz2" \
        || echo "    Voice download failed - replies will use espeak-ng instead."
else
    echo "    Already present."
fi

chmod +x "$HERE/bench"

cat <<EOF

Setup done. Reboot once so the group changes and services take effect:

    sudo reboot

Then reconnect over SSH and run:

    cd $REPO
    ./scripts/pocket/bench all

EOF
