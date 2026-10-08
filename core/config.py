"""
Settings and shared constants. Everything user-tunable lives here.
"""

import os
import platform
import sys

import anthropic
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

# ── Claude ────────────────────────────────────────────────────────────────────
MODEL         = "claude-haiku-4-5-20251001"
client        = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

# ── Voice ─────────────────────────────────────────────────────────────────────
# VOICE_NAME: fragment of an installed SAPI voice, e.g. "David", "Zira" (empty = system default)
# Install voices: Settings -> Time & Language -> Speech -> Add voices
VOICE_NAME  = ""
SPEECH_RATE = 1        # -10 (slow) to 10 (fast)

# ── Wake word / audio ─────────────────────────────────────────────────────────
WAKE_WORD   = "jarvis"
SAMPLE_RATE = 16000    # 16 kHz — matches Vosk, Whisper and WebRTC VAD
FRAME       = 320      # 20 ms at 16 kHz — exact frame size webrtcvad requires
# Restrict Vosk to the wake word only (less CPU). Off by default: in testing it
# mapped unrelated room conversation onto "jarvis" and caused false wakes.
WAKE_GRAMMAR = False
# After the wake word, how long to wait for you to keep talking ("Jarvis, open Discord")
# before falling back to "Yes sir" and a separate listen.
FOLLOWUP_WINDOW = 0.5  # seconds
# Microphone: name fragment to pin a specific input (e.g. "AirPods"). Empty = follow the
# Windows default recording device, switching automatically when it changes.
INPUT_DEVICE = ""

# ── Paths ─────────────────────────────────────────────────────────────────────
WORKSPACES_FILE   = os.path.join(ROOT, "workspaces.json")
VOSK_MODEL_DIR    = os.path.join(ROOT, "models", "vosk-model-small-en-us-0.15")
DASH_BUILD        = os.path.join(ROOT, "jarvis-dashboard", "build")
STATIC_DIR        = os.path.join(ROOT, "core", "static")
JARVIS_INPUT_DIR  = os.path.join(ROOT, "jarvis_input")
JARVIS_OUTPUT_DIR = os.path.join(ROOT, "jarvis_output")
INPUT_ARCHIVE_DIR = os.path.join(JARVIS_INPUT_DIR, "archive")
LOG_DIR           = os.path.join(ROOT, "logs")
CODING_DIR        = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "jarvis_coding")

# External tools
MPV_EXE    = r"C:\Users\paten\OneDrive\Desktop\Tools\mpv\mpv.exe"   # or just "mpv" if it's on PATH
# Real-ESRGAN upscaler (runs on any Vulkan GPU); Pillow is used as a fallback when missing.
# Download from: https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases
ESRGAN_EXE = r"C:\Users\paten\OneDrive\Desktop\Tools\ESRGAN\realesrgan-ncnn-vulkan.exe"

# ── Server ────────────────────────────────────────────────────────────────────
PORT = 5151

# ── Background (24/7) mode ────────────────────────────────────────────────────
# Off during development. Enable with:  python jarvis.py --background
# (scripts/install_startup.ps1 registers exactly that command to run at login).
# When on: no browser on startup, all output goes to logs/jarvis.log, and the
# file/bookmark/app indexes refresh periodically.
BACKGROUND_MODE      = "--background" in sys.argv
INDEX_REFRESH_SECS   = 60 * 60

OS = platform.system()
