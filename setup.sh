#!/bin/bash
# JARVIS Setup Script

set -e

echo "Installing JARVIS dependencies..."

pip install anthropic python-dotenv pyautogui pygetwindow Pillow \
    sounddevice numpy faster-whisper soundfile vosk requests \
    flask flask-cors webrtcvad resemblyzer

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
echo "Creating folders..."
mkdir -p jarvis_input jarvis_output

echo ""
if [ ! -f ".env" ]; then
    cat > .env <<EOF
ANTHROPIC_API_KEY=sk-ant-...
BRAVE_API_KEY=BSA...
EOF
    echo "Created .env — fill in your API keys before running."
else
    echo ".env already exists, skipping."
fi

echo ""
echo "Setup complete. Run with:"
echo "  python jarvis.py"
