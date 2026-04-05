#!/bin/bash
# JARVIS Setup Script

echo "Installing JARVIS dependencies..."

# Python packages
pip install anthropic pyaudio numpy SpeechRecognition

# On macOS, pyaudio needs portaudio first:
# brew install portaudio
# pip install pyaudio

# On Ubuntu/Debian:
# sudo apt-get install portaudio19-dev python3-pyaudio

echo ""
echo "Set your API key:"
echo "  export ANTHROPIC_API_KEY='sk-ant-...'"
echo ""
echo "Then run:"
echo "  python jarvis.py"
