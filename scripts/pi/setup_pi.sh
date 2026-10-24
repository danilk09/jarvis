#!/bin/bash
# Sets up Jarvis's headless brain on the Raspberry Pi 5 (64-bit Raspberry Pi OS Lite).
# Run it from the jarvis folder on the Pi; it is safe to run again after pulling changes:
#
#     cd ~/jarvis && bash scripts/pi/setup_pi.sh
#
# Then copy your data from the PC (scripts/pi/push_to_pi.ps1, run on the PC) and install the service:
#     bash scripts/pi/install_service.sh

set -e
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"

echo "== System packages"
# ffmpeg: phone recordings → WAV for Whisper. nodejs/npm: yt-dlp's YouTube challenges and the
# npx-based MCP servers. tmux: "continue this on my phone" (Claude Code Remote Control) and long
# diagnostics runs. stress-ng / fio / espeak-ng: scripts/pi/diag (heat, SD card, load tests).
PKGS="git python3-venv python3-dev build-essential ffmpeg tmux curl stress-ng fio espeak-ng"
# Node only if it's missing: NodeSource's nodejs (often already installed) ships its own npm and
# conflicts with Debian's npm package, which makes apt refuse the whole install
command -v node >/dev/null 2>&1 || PKGS="$PKGS nodejs"
command -v npm >/dev/null 2>&1 || PKGS="$PKGS npm"
sudo apt-get update
sudo apt-get install -y $PKGS

echo "== uv (runs the Python MCP servers with uvx)"
if ! command -v uvx >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uvx" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "== Python environment (.venv)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip
# No sounddevice/vosk (no mic on the Pi), no pycaw/comtypes (Windows only).
# webrtcvad-wheels: same `webrtcvad` module with prebuilt wheels; the original imports
# pkg_resources, which new setuptools removed (it would silently fall back to a cruder VAD).
.venv/bin/pip install anthropic python-dotenv Pillow numpy faster-whisper requests \
    flask flask-cors webrtcvad-wheels "yt-dlp[default]" pypdf trafilatura sherpa-onnx

echo "== Whisper model (downloaded once, ~150 MB)"
.venv/bin/python - <<'EOF'
from core import config
from faster_whisper import WhisperModel
WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
print("  Whisper ready:", config.WHISPER_MODEL)
EOF

echo "== Speaker model (voice ID)"
mkdir -p models/speaker
if [ ! -f models/speaker/nemo_en_titanet_small.onnx ]; then
    curl -L -o models/speaker/nemo_en_titanet_small.onnx \
        https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
fi

echo "== Jarvis's voice (Piper: replies are spoken on the phone page)"
mkdir -p models/tts
if [ ! -d "models/tts/vits-piper-en_GB-northern_english_male-medium" ]; then
    curl -L -o models/tts/voice.tar.bz2 \
        https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-en_GB-northern_english_male-medium.tar.bz2
    tar xjf models/tts/voice.tar.bz2 -C models/tts/
    rm models/tts/voice.tar.bz2
fi

echo "== Folders"
mkdir -p jarvis_input jarvis_output logs "$HOME/GitHub" "$HOME/jarvis_coding"

echo "== Tailscale HTTPS (the phone page needs it for the mic)"
if command -v tailscale >/dev/null 2>&1; then
    # lets Jarvis run `tailscale cert` without sudo
    sudo tailscale set --operator="$USER" && echo "  $USER may now request Tailscale certificates"
else
    echo "  Tailscale isn't installed yet (runbook step 3)."
fi

echo "== Claude Code"
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
    echo "  found"
else
    echo "  not installed: curl -fsSL https://claude.ai/install.sh | bash   (then run claude once to sign in)"
fi

echo
[ -f .env ] || echo "No .env yet: on the PC, run  powershell -File scripts\\pi\\push_to_pi.ps1 <user>@<pi>"
echo "Done. Try it in the foreground first:   .venv/bin/python jarvis.py"
echo "Then run it as a service:               bash scripts/pi/install_service.sh"
echo "Health checks:                          ./scripts/pi/diag check"
