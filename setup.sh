#!/bin/bash
# JARVIS Setup Script

set -e

echo "Installing JARVIS dependencies..."

pip install anthropic python-dotenv Pillow \
    sounddevice numpy faster-whisper vosk requests \
    flask flask-cors webrtcvad "yt-dlp[default]" pycaw comtypes pypdf trafilatura sherpa-onnx

echo ""
echo "Downloading Vosk speech model..."
mkdir -p models
if [ ! -d "models/vosk-model-small-en-us-0.15" ]; then
    curl -L -o models/vosk-model-small-en-us-0.15.zip \
        https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
    unzip -q models/vosk-model-small-en-us-0.15.zip -d models/
    rm models/vosk-model-small-en-us-0.15.zip
    echo "  Vosk model downloaded."
else
    echo "  Vosk model already present, skipping."
fi

echo ""
echo "Downloading the speaker model (voice ID: Jarvis answers only your voice)..."
mkdir -p models/speaker
if [ ! -f "models/speaker/nemo_en_titanet_small.onnx" ]; then
    curl -L -o models/speaker/nemo_en_titanet_small.onnx \
        https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
    echo "  Speaker model downloaded. Then say: \"Jarvis, learn my voice\""
else
    echo "  Speaker model already present, skipping."
fi

echo ""
echo "Installing the desktop app (Electron — lets the Stage show live web pages)..."
if command -v npm >/dev/null 2>&1; then
    (cd desktop && npm install && node node_modules/electron/install.js)
else
    echo "  npm not found — install Node.js, then run: cd desktop && npm install"
    echo "  (Jarvis falls back to opening the dashboard in your browser.)"
fi

echo ""
if command -v claude >/dev/null 2>&1; then
    echo "Claude Code found — coding projects will use it."
else
    echo "Optional — coding projects: install Claude Code and log in once:"
    echo "  npm install -g @anthropic-ai/claude-code && claude"
fi

echo ""
echo "Creating folders..."
mkdir -p jarvis_input jarvis_output

echo ""
if [ ! -f ".env" ]; then
    cat > .env <<EOF
ANTHROPIC_API_KEY=sk-ant-...
BRAVE_API_KEY=BSA...
# Optional, free at https://ion.cesium.com — 3-D terrain and buildings on the Stage globe
CESIUM_ION_TOKEN=
EOF
    echo "Created .env — fill in your API keys before running."
else
    echo ".env already exists, skipping."
fi

echo ""
echo "Optional — music playback:"
echo "  Download mpv (shinchiro Windows build) from https://mpv.io/installation/"
echo "  Extract mpv-x86_64-*.7z and add the folder to PATH,"
echo "  or set MPV_EXE in jarvis.py to the full path of mpv.exe."
echo ""
echo "Setup complete. Run with:"
echo "  python jarvis.py"
