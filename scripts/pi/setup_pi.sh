#!/bin/bash
# Sets up Jarvis's headless brain on the Raspberry Pi 5 (64-bit Raspberry Pi OS Lite).
# Run it from the jarvis folder on the Pi; it is safe to run again after pulling changes:
#
#     cd ~/jarvis && bash scripts/pi/setup_pi.sh
#
# Then copy your data from the PC (runbook step 9) and install the service:
#     bash scripts/pi/install_service.sh

set -e
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"

echo "== System packages"
# ffmpeg: phone recordings → WAV for Whisper. nodejs/npm: yt-dlp's YouTube challenges and the
# npx-based MCP servers. tmux: "continue this on my phone" (Claude Code Remote Control).
sudo apt-get update
sudo apt-get install -y git python3-venv python3-dev build-essential ffmpeg nodejs npm tmux curl

echo "== uv (runs the Python MCP servers with uvx)"
if ! command -v uvx >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uvx" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "== Python environment (.venv)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip
# No sounddevice/vosk (no mic on the Pi), no pycaw/comtypes (Windows only)
.venv/bin/pip install anthropic python-dotenv Pillow numpy faster-whisper requests \
    flask flask-cors webrtcvad "yt-dlp[default]" pypdf trafilatura sherpa-onnx

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
[ -f .env ] || echo "No .env yet: copy it from the PC (runbook step 9)."
echo "Done. Try it in the foreground first:   .venv/bin/python jarvis.py"
echo "Then run it as a service:               bash scripts/pi/install_service.sh"
