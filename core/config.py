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
# Restrict Vosk to the wake word plus a few sound-alike "decoy" words. Much lighter than
# full-vocabulary recognition, which in a loud room fell further and further behind real
# time (minutes, in testing) until the wake word stopped working. The decoys soak up
# near-misses, and Whisper re-checks every wake anyway. Set False for full vocabulary.
WAKE_GRAMMAR = True
WAKE_DECOYS  = ["service", "nervous", "harvest", "travis", "mavis", "jars", "purpose", "office"]
# After the wake word, how long to wait for you to keep talking ("Jarvis, open Discord")
# before falling back to "Yes sir" and a separate listen.
FOLLOWUP_WINDOW = 0.9  # seconds (a natural pause after "Jarvis," is ~0.3 s)
# Speech-to-text model (faster-whisper). "base.en" is English-only: same speed as "base" but
# hears "Jarvis" much more reliably in background noise. "small.en" is more accurate, ~3x slower.
WHISPER_MODEL = "base.en"
# How much louder than the room's background noise your voice must be to count as speech
# when deciding you've stopped talking (2.0 ≈ +6 dB). Raise it if recordings run on in a
# loud place; lower it if Jarvis cuts you off when you speak quietly.
SPEECH_OVER_NOISE = 2.0
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

# ── Claude Code (coding projects, see core/coding.py) ─────────────────────────
# Your coding preferences live in CODING_DIR/CLAUDE.md — say "remember I prefer ..." to add one.
CLAUDE_EXE           = ""              # path to claude.exe; empty = find it on PATH
CODE_PERMISSION_MODE = "acceptEdits"   # file edits run automatically; commands must be allowed below
CODE_MODEL           = ""              # "" = Claude Code's default; or "opus", "sonnet", "fable", "haiku"
CODE_EFFORT          = ""              # "" = default; or "low", "medium", "high", "xhigh", "max"
# False: Claude Code uses your own Claude login/subscription (`claude` → /login). True: it bills
# the ANTHROPIC_API_KEY from .env instead (that key would otherwise override your login).
CODE_USE_API_KEY     = False
# Commands Claude Code may run without asking (it runs in the background, so nobody can click
# "approve"). Anything else is refused and Jarvis asks you — say "allow it" to run it.
_ALLOWED_COMMANDS = [
    "npm install", "npm ci", "npm run", "npm test", "npm init", "npm create", "npm ls",
    "pnpm install", "pnpm add", "pnpm run", "pnpm test", "pnpm create",
    "yarn install", "yarn add", "yarn run", "yarn test", "yarn create",
    "pip install", "python -m pip install", "python -m venv", "python -m pytest", "pytest",
    "py -m pip install", "py -m venv", "py -m pytest",
    "git init", "git status", "git diff", "git log", "git add", "git commit", "git branch", "git checkout",
    "node --version", "python --version", "npm --version",
    # running and testing the project's own code, and moving around inside it
    "python", "python3", "py", "node", "cd", "Set-Location", "mkdir",
]
CODE_ALLOWED_TOOLS = [f"{shell}({cmd}:*)" for cmd in _ALLOWED_COMMANDS for shell in ("PowerShell", "Bash")]

# External tools
MPV_EXE    = r"C:\Users\paten\OneDrive\Desktop\Tools\mpv\mpv.exe"   # or just "mpv" if it's on PATH
# Real-ESRGAN upscaler (runs on any Vulkan GPU); Pillow is used as a fallback when missing.
# Download from: https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases
ESRGAN_EXE = r"C:\Users\paten\OneDrive\Desktop\Tools\ESRGAN\realesrgan-ncnn-vulkan.exe"

# ── Server / desktop app ──────────────────────────────────────────────────────
PORT = 5151
# Open the dashboard in the Electron desktop app (desktop/) instead of a browser tab.
# The desktop app is what lets the Stage show live web pages. Falls back to the
# browser when the app isn't installed (cd desktop && npm install).
USE_DESKTOP_APP = True
DESKTOP_DIR     = os.path.join(ROOT, "desktop")
# The Stage is saved here and restored on the next start ("clear the stage" resets it)
STAGE_STATE_FILE = os.path.join(ROOT, "stage_state.json")
# Optional, free at cesium.com/ion: 3-D terrain, buildings and photorealistic cities on the map
CESIUM_ION_TOKEN = os.environ.get("CESIUM_ION_TOKEN", "")

# ── Background (24/7) mode ────────────────────────────────────────────────────
# Off during development. Enable with:  python jarvis.py --background
# (scripts/install_startup.ps1 registers exactly that command to run at login).
# When on: no browser on startup, all output goes to logs/jarvis.log, and the
# file/bookmark/app indexes refresh periodically.
BACKGROUND_MODE      = "--background" in sys.argv
INDEX_REFRESH_SECS   = 60 * 60

OS = platform.system()
