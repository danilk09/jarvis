#!/usr/bin/env python3
"""
JARVIS - Voice-Activated AI Assistant
Say Jarvis -> speak your command -> local AI figures out what to do
"""

import anthropic
from dotenv import load_dotenv
import concurrent.futures
import os
import sys
import json
import time
import subprocess
import webbrowser
import platform
import threading
import queue as _queue
import glob
import sqlite3
import shutil
import tempfile
import requests
import vosk
import base64
import re
from flask import Flask as _Flask, jsonify as _jsonify, request as _freq, send_from_directory as _sfd
from flask_cors import CORS as _CORS

try:
    from PIL import ImageGrab
except ImportError:
    print("Missing dependencies. Run: pip install Pillow")
    sys.exit(1)

try:
    import sounddevice as sd
    import numpy as np
    from faster_whisper import WhisperModel
    import soundfile as sf
except ImportError:
    print("Missing dependencies. Run: pip install sounddevice numpy faster-whisper soundfile")
    sys.exit(1)

try:
    import webrtcvad as _webrtcvad
    _WEBRTCVAD_OK = True
except ImportError:
    _WEBRTCVAD_OK = False
    print("  webrtcvad not installed — falling back to amplitude VAD. Run setup.sh to fix.")


load_dotenv()
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

print("  Loading Whisper model (first run may take a moment)...")
WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")

# ── Voice Engine ──────────────────────────────────────────────────────────────
# SPEECH_RATE: -10 (slow) to 10 (fast)
# VOICE_NAME: "Microsoft Guy" "Microsoft Davis" "Microsoft David" "Microsoft Zira"
# Install voices: Settings -> Time & Language -> Speech -> Add voices
VOICE_NAME  = ""
SPEECH_RATE = 1

_tts_queue = _queue.Queue()
_tts_ready = threading.Event()
_current_tts_proc = None
_tts_proc_lock = threading.Lock()
_tts_suppressed = threading.Event()
_phone_command_queue = _queue.Queue()
_whisper_lock = threading.Lock()

def _tts_worker():
    global _current_tts_proc
    _tts_ready.set()
    while True:
        item = _tts_queue.get()
        if item is None:
            break
        text, update_state = item if isinstance(item, tuple) else (item, True)
        try:
            voice_line = f"$s.Voice = $s.GetVoices() | Where-Object {{$_.GetAttribute('Name') -like '*{VOICE_NAME}*'}} | Select-Object -First 1;" if VOICE_NAME else ""
            ps_cmd = (
                f"$s = New-Object -ComObject SAPI.SpVoice;"
                f"{voice_line}"
                f"$s.Rate = {SPEECH_RATE};"
                f"$s.Speak([System.String]::Concat('{text}')) | Out-Null"
            )
            if update_state:
                _start_word_beats(text)
            with _tts_proc_lock:
                _current_tts_proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            _current_tts_proc.wait()
            with _tts_proc_lock:
                _current_tts_proc = None
            if update_state:
                _stop_word_beats()
        except Exception as e:
            print(f"  TTS error: {e}")
            _stop_word_beats()
        if update_state and _tts_queue.empty():
            _push_state("idle")
        _tts_queue.task_done()

_tts_thread = threading.Thread(target=_tts_worker, daemon=True)
_tts_thread.start()
_tts_ready.wait()

def stop_tts():
    """Kill the current TTS subprocess and drain the queue."""
    global _current_tts_proc
    _tts_suppressed.set()
    with _tts_proc_lock:
        if _current_tts_proc and _current_tts_proc.poll() is None:
            try:
                _current_tts_proc.terminate()
                _current_tts_proc.wait(timeout=1)
            except Exception:
                pass
            _current_tts_proc = None
    while True:
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except _queue.Empty:
            break

def speak(text, update_state=True):
    if _tts_suppressed.is_set():
        print(f"  [muted] JARVIS: {text}")
        return
    print(f"  JARVIS: {text}")
    safe = text.replace("'", "''").replace("`", "").replace("$", "").replace(";", ",")
    _tts_queue.put((safe, update_state))

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACES_FILE = os.path.join(os.path.dirname(__file__), "workspaces.json")
SAMPLE_RATE     = 16000   # 16 kHz — matches Whisper and WebRTC VAD requirements
CHUNK           = 320     # 20 ms at 16 kHz — exact frame size required by webrtcvad

OS              = platform.system()

# Folders for image I/O — created at startup if missing
JARVIS_INPUT_DIR  = os.path.join(os.path.dirname(__file__), "jarvis_input")
JARVIS_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "jarvis_output")
CODING_DIR        = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "jarvis_coding")

# ── Embedded dashboard server ─────────────────────────────────────────────────
_DASH_BUILD = os.path.join(os.path.dirname(__file__), "jarvis-dashboard", "build")
_dash_lock  = threading.Lock()
_dash_state: dict = {
    "state":       "idle",
    "transcript":  "",
    "files":       [],
    "speech_beat": 0,
}

_flask = _Flask(__name__, static_folder=_DASH_BUILD, static_url_path="")
_CORS(_flask)

_TAILSCALE_CERT_PATH = ""
_TAILSCALE_KEY_PATH  = ""
_tailscale_url       = ""

def _tailscale_exe():
    """Return the tailscale CLI path, checking common Windows install locations."""
    candidates = [
        "tailscale",
        r"C:\Program Files\Tailscale\tailscale.exe",
        r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    ]
    for c in candidates:
        try:
            r = subprocess.run([c, "version"], capture_output=True, timeout=3)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None

def _detect_tailscale():
    """Returns (ip, fqdn) e.g. ('100.x.x.x', 'my-pc.tail1234.ts.net') or (None, None)."""
    exe = _tailscale_exe()
    if not exe:
        return None, None
    try:
        ip = subprocess.run(
            [exe, "ip", "-4"],
            capture_output=True, text=True, timeout=4
        ).stdout.strip()
        if not ip or not ip.startswith("100."):
            return None, None
        data = json.loads(subprocess.run(
            [exe, "status", "--json"],
            capture_output=True, text=True, timeout=4
        ).stdout)
        fqdn = data.get("Self", {}).get("DNSName", "").rstrip(".")
        return ip, fqdn or None
    except Exception:
        return None, None

def _setup_tailscale_https(fqdn):
    """Run 'tailscale cert <fqdn>' and return (cert_path, key_path) or (None, None)."""
    exe = _tailscale_exe()
    if not exe:
        return None, None
    base = os.path.dirname(os.path.abspath(__file__))
    cert = os.path.join(base, f"{fqdn}.crt")
    key  = os.path.join(base, f"{fqdn}.key")
    try:
        subprocess.run(
            [exe, "cert", fqdn],
            capture_output=True, text=True, timeout=15, cwd=base
        )
        if os.path.exists(cert) and os.path.exists(key):
            return cert, key
    except Exception:
        pass
    return None, None

@_flask.route("/status")
def _route_status():
    with _dash_lock:
        return _jsonify(dict(_dash_state))

@_flask.route("/", defaults={"path": ""})
@_flask.route("/<path:path>")
def _route_dashboard(path):
    full = os.path.join(_DASH_BUILD, path)
    if path and os.path.exists(full):
        return _sfd(_DASH_BUILD, path)
    return _sfd(_DASH_BUILD, "index.html")

_PHONE_PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>Jarvis Mic</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0a0a0f; color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; min-height: 100vh; padding: 24px;
      -webkit-tap-highlight-color: transparent;
    }
    h1 { font-size: 1.2rem; letter-spacing: 0.3em; color: #5c9ce6; margin-bottom: 40px; font-weight: 300; }
    #btn {
      width: 180px; height: 180px; border-radius: 50%;
      border: 2px solid #1e3a5f; background: #111827;
      color: #5c9ce6; font-size: 0.8rem; letter-spacing: 0.15em; font-weight: 500;
      cursor: pointer; transition: all 0.2s; outline: none;
      -webkit-user-select: none; user-select: none;
      display: flex; align-items: center; justify-content: center;
      line-height: 1.6; text-align: center;
    }
    #btn:active { transform: scale(0.96); }
    #btn.recording { background: #1a0a0a; border-color: #c0392b; color: #e74c3c; animation: pulse 1.4s ease-in-out infinite; }
    #btn.sending   { background: #0a1a0a; border-color: #27ae60; color: #2ecc71; cursor: default; }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(231,76,60,0.3); }
      50%       { box-shadow: 0 0 0 20px rgba(231,76,60,0); }
    }
    #status  { margin-top: 32px; font-size: 0.85rem; color: #666; min-height: 1.4em; text-align: center; letter-spacing: 0.05em; }
    #command { margin-top: 14px; font-size: 0.9rem; color: #9e9e9e; min-height: 1.2em; max-width: 300px; text-align: center; font-style: italic; line-height: 1.5; }
    .error   { color: #e74c3c !important; }
    .success { color: #2ecc71 !important; }
  </style>
</head>
<body>
  <h1>J A R V I S</h1>
  <button id="btn">TAP TO<br>SPEAK</button>
  <div id="status">Ready</div>
  <div id="command"></div>
  <script>
    const btn = document.getElementById('btn');
    const statusEl = document.getElementById('status');
    const cmdEl = document.getElementById('command');
    let recorder = null, chunks = [], recording = false;

    btn.addEventListener('click', async () => {
      if (btn.classList.contains('sending')) return;
      if (recording) { stopRecording(); return; }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
        chunks = [];
        recorder = new MediaRecorder(stream);
        recorder.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data); };
        recorder.onstop = () => { stream.getTracks().forEach(t => t.stop()); sendAudio(); };
        recorder.start();
        recording = true;
        btn.innerHTML = 'TAP TO<br>STOP';
        btn.classList.add('recording');
        statusEl.textContent = 'Recording…';
        statusEl.className = '';
        cmdEl.textContent = '';
        cmdEl.className = '';
      } catch(e) {
        statusEl.textContent = 'Microphone access denied.';
        statusEl.className = 'error';
      }
    });

    function stopRecording() {
      if (recorder && recorder.state !== 'inactive') recorder.stop();
      recording = false;
      btn.innerHTML = '···';
      btn.classList.remove('recording');
      btn.classList.add('sending');
      statusEl.textContent = 'Sending…';
    }

    async function sendAudio() {
      const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
      const form = new FormData();
      form.append('audio', blob, 'command.webm');
      try {
        const res = await fetch('/phone-command', { method: 'POST', body: form });
        const data = await res.json();
        if (res.ok) {
          statusEl.textContent = 'Command sent!';
          statusEl.className = 'success';
          cmdEl.textContent = '\\u201c' + data.command + '\\u201d';
        } else {
          statusEl.textContent = 'Error: ' + (data.error || 'Unknown');
          statusEl.className = 'error';
        }
      } catch(e) {
        statusEl.textContent = 'Network error — same Wi-Fi?';
        statusEl.className = 'error';
      }
      btn.innerHTML = 'TAP TO<br>SPEAK';
      btn.classList.remove('sending');
    }
  </script>
</body>
</html>"""

@_flask.route("/phone")
def _route_phone():
    return _PHONE_PAGE, 200, {"Content-Type": "text/html; charset=utf-8"}

@_flask.route("/phone-command", methods=["POST"])
def _route_phone_command():
    audio_file = _freq.files.get("audio")
    if not audio_file:
        return _jsonify({"error": "no audio"}), 400

    ct = audio_file.content_type or ""
    suffix = ".wav" if "wav" in ct else ".mp4" if ("mp4" in ct or "m4a" in ct) else ".webm"

    src_fd, src_path = tempfile.mkstemp(suffix=suffix)
    wav_path = src_path.rsplit(".", 1)[0] + "_16k.wav"
    try:
        with os.fdopen(src_fd, "wb") as fh:
            audio_file.save(fh)

        r = subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            return _jsonify({"error": "audio conversion failed"}), 500

        with _whisper_lock:
            segments, _ = WHISPER_MODEL.transcribe(
                wav_path, language="en", vad_filter=True,
                initial_prompt="Open Discord, search YouTube for, open workspace, never mind, stop, yes, no, cancel, open file, find files, take a screenshot, analyze image, what's on screen",
                vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
            )
        command = " ".join(s.text.strip() for s in segments).strip()
        if not command:
            return _jsonify({"error": "no speech detected"}), 400

        _phone_command_queue.put(command)
        print(f'  Phone command queued: "{command}"')
        return _jsonify({"command": command, "status": "processing"})
    except Exception as e:
        return _jsonify({"error": str(e)}), 500
    finally:
        for p in [src_path, wav_path]:
            try:
                os.remove(p)
            except Exception:
                pass

def _start_embedded_server():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    ssl_ctx = (_TAILSCALE_CERT_PATH, _TAILSCALE_KEY_PATH) if _TAILSCALE_CERT_PATH else None
    _flask.run(host="0.0.0.0", port=5151, debug=False, threaded=True,
               use_reloader=False, ssl_context=ssl_ctx)

# ── File Index ────────────────────────────────────────────────────────────────
FILE_INDEX = []

def build_file_index():
    global FILE_INDEX
    print("  Building file index (scanning your directories)...")
    roots = [
        os.path.expanduser("~/Desktop"),
        os.path.expanduser("~/Documents"),
        os.path.expanduser("~/Downloads"),
        os.path.expanduser("~/Pictures"),
        os.path.expanduser("~/Music"),
        os.path.expanduser("~/Videos"),
        os.path.expanduser("~/OneDrive"),
        os.path.expanduser("~/Google Drive"),
    ]
    paths = []
    for root in roots:
        if os.path.exists(root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in ['node_modules', '__pycache__', '.git']]
                for f in filenames:
                    paths.append(os.path.join(dirpath, f))
    FILE_INDEX = paths
    print(f"  File index built: {len(FILE_INDEX)} files found.")

def get_chrome_bookmarks():
    bookmarks = []
    chrome_path = os.path.expanduser("~/AppData/Local/Google/Chrome/User Data/Default/Bookmarks")
    if not os.path.exists(chrome_path):
        return bookmarks
    try:
        with open(chrome_path, encoding="utf-8") as f:
            data = json.load(f)
        def extract(node):
            if node.get("type") == "url":
                bookmarks.append({"name": node.get("name",""), "url": node.get("url","")})
            for child in node.get("children", []):
                extract(child)
        for root in data.get("roots", {}).values():
            extract(root)
    except Exception as e:
        print(f"  Could not read Chrome bookmarks: {e}")
    return bookmarks

def get_edge_bookmarks():
    bookmarks = []
    edge_path = os.path.expanduser("~/AppData/Local/Microsoft/Edge/User Data/Default/Bookmarks")
    if not os.path.exists(edge_path):
        return bookmarks
    try:
        with open(edge_path, encoding="utf-8") as f:
            data = json.load(f)
        def extract(node):
            if node.get("type") == "url":
                bookmarks.append({"name": node.get("name",""), "url": node.get("url","")})
            for child in node.get("children", []):
                extract(child)
        for root in data.get("roots", {}).values():
            extract(root)
    except Exception as e:
        print(f"  Could not read Edge bookmarks: {e}")
    return bookmarks

BOOKMARKS = []

def build_bookmark_index():
    global BOOKMARKS
    BOOKMARKS = get_chrome_bookmarks() + get_edge_bookmarks()
    print(f"  Bookmark index built: {len(BOOKMARKS)} bookmarks found.")

# ── Browser History ───────────────────────────────────────────────────────────
HISTORY = []

def read_browser_history(db_path, limit=5000):
    entries = []
    if not os.path.exists(db_path):
        return entries
    tmp = os.path.join(tempfile.gettempdir(), "jarvis_history_tmp.db")
    try:
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        cur  = conn.cursor()
        cur.execute("""
            SELECT title, url, last_visit_time
            FROM urls
            ORDER BY last_visit_time DESC
            LIMIT ?
        """, (limit,))
        epoch_offset = 11644473600
        for title, url, ts in cur.fetchall():
            try:
                secs = ts / 1_000_000 - epoch_offset
                visited = time.strftime("%Y-%m-%d %H:%M", time.localtime(secs))
            except Exception:
                visited = "unknown"
            entries.append({"title": title or "", "url": url, "visited": visited})
        conn.close()
    except Exception as e:
        print(f"  Could not read history from {db_path}: {e}")
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return entries

def build_history_index():
    global HISTORY
    paths = [
        os.path.expanduser("~/AppData/Local/Google/Chrome/User Data/Default/History"),
        os.path.expanduser("~/AppData/Local/Microsoft/Edge/User Data/Default/History"),
    ]
    all_entries = []
    for p in paths:
        all_entries.extend(read_browser_history(p))
    seen = {}
    for e in all_entries:
        url = e["url"]
        if url not in seen or e["visited"] > seen[url]["visited"]:
            seen[url] = e
    HISTORY = sorted(seen.values(), key=lambda x: x["visited"], reverse=True)
    print(f"  History index built: {len(HISTORY)} unique pages found.")

def search_history(keyword=None, days_ago=None, limit=5):
    results = HISTORY
    if days_ago is not None:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days_ago * 86400))
        results = [e for e in results if e["visited"] >= cutoff]
    if keyword:
        kw = keyword.lower()
        results = [e for e in results if kw in e["title"].lower() or kw in e["url"].lower()]
    return results[:limit]

# ── Workspaces ────────────────────────────────────────────────────────────────
def load_workspaces():
    if os.path.exists(WORKSPACES_FILE):
        with open(WORKSPACES_FILE) as f:
            return json.load(f)
    return {}

def open_workspace(name, workspaces):
    ws = workspaces.get(name)
    if not ws:
        for key in workspaces:
            if name.lower() in key.lower() or key.lower() in name.lower():
                ws = workspaces[key]
                name = key
                break
    if not ws:
        return False, f"No workspace named '{name}' found."
    opened = []
    for item in ws.get("items", []):
        kind = item.get("type")
        path = item.get("path", "")
        if kind == "url":
            webbrowser.open(path)
            opened.append(f"URL: {path}")
        elif kind == "vscode":
            subprocess.Popen(["code", path], shell=True)
            opened.append(f"VSCode: {path}")
        elif kind == "file":
            os.startfile(path)
            opened.append(f"File: {path}")
        elif kind == "app":
            subprocess.Popen(f'start "" "{path}"', shell=True)
            opened.append(f"App: {path}")
        time.sleep(0.3)
    return True, f"Opened workspace '{name}': {', '.join(opened)}"

# ── System Actions ─────────────────────────────────────────────────────────────
def open_app(name):
    try:
        safe_name = re.sub(r"[`$;{}\"'\\]", "", name)
        ps_cmd = (
            f"$app = Get-StartApps | Where-Object {{ $_.Name -like '*{safe_name}*' }} | "
            f"Select-Object -First 1; "
            f"if ($app) {{ Start-Process \"shell:AppsFolder\\$($app.AppID)\" }}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=8
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass
    try:
        subprocess.Popen([name], shell=True)
        return True
    except Exception:
        pass
    return False

def fuzzy_match_files(keyword, file_index, threshold=60, limit=20):
    keyword = keyword.lower()
    kw_words = set(keyword.split())
    scored = []
    for p in file_index:
        name = os.path.basename(p).lower()
        name_no_ext = os.path.splitext(name)[0]
        if keyword in name:
            scored.append((100, p))
            continue
        name_words = set(name_no_ext.replace("_", " ").replace("-", " ").split())
        overlap = len(kw_words & name_words)
        if overlap == 0:
            continue
        shorter, longer = sorted([keyword, name_no_ext], key=len)
        char_score = sum(1 for c in shorter if c in longer) / max(len(longer), 1) * 100
        score = (overlap / max(len(kw_words), 1) * 60) + (char_score * 0.4)
        if score >= threshold:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]

# ── Image Analysis ────────────────────────────────────────────────────────────
def image_to_base64(path):
    """Read an image file and return (base64_str, media_type)."""
    ext = os.path.splitext(path)[1].lower()
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp"}
    media_type = media_map.get(ext, "image/png")
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8"), media_type

def analyze_image_with_claude(image_path, prompt):
    """
    Send an image to Claude Haiku for analysis.
    Returns the text response. Does NOT add the image to CHAT_HISTORY
    to avoid re-sending the base64 blob on every subsequent call.
    Instead, the text summary is added to history so Jarvis can refer back to it.
    """
    b64, media_type = image_to_base64(image_path)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="You are a voice assistant. Answer only what was asked about the image in 1-3 concise sentences. No markdown, no bullet points, no headers — plain spoken sentences only.",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64}
                    },
                    {"type": "text", "text": prompt or "Describe this image concisely."}
                ]
            }]
        )
        result = response.content[0].text.strip()
        with _chat_lock:
            CHAT_HISTORY.append({"role": "user", "content": f"[Image analysis request] {prompt}"})
            CHAT_HISTORY.append({"role": "assistant", "content": f"[Image analysis result] {result}"})
        return result
    except Exception as e:
        return f"Image analysis failed: {e}"

def take_screenshot(prompt):
    """Capture fullscreen, save to jarvis_output/, analyze with Claude."""
    os.makedirs(JARVIS_OUTPUT_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(JARVIS_OUTPUT_DIR, f"screenshot_{timestamp}.png")
    try:
        img = ImageGrab.grab()
        img.save(path)
        print(f"  Screenshot saved: {path}")
    except Exception as e:
        return f"Screenshot failed: {e}"
    result = analyze_image_with_claude(path, prompt or "Describe what's on screen.")
    return result

def _pillow_enhance(image_path, prompt):
    """Pillow-based enhancement fallback when Real-ESRGAN exe isn't available."""
    from PIL import Image, ImageEnhance
    try:
        b64, media_type = image_to_base64(image_path)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": (
                    f"User wants: '{prompt}'. Return ONLY JSON: "
                    '{"brightness":1.0,"contrast":1.0,"sharpness":1.0,"color":1.0,"description":"..."} '
                    "1.0=no change, range 0.5-2.0 (sharpness up to 3.0)."
                )}
            ]}]
        )
        raw = response.content[0].text.strip()
        params = json.loads(raw[raw.find("{"):raw.rfind("}")+1])
        img = Image.open(image_path).convert("RGB")
        img = ImageEnhance.Brightness(img).enhance(params.get("brightness", 1.0))
        img = ImageEnhance.Contrast(img).enhance(params.get("contrast", 1.0))
        img = ImageEnhance.Sharpness(img).enhance(params.get("sharpness", 1.0))
        img = ImageEnhance.Color(img).enhance(params.get("color", 1.0))
        os.makedirs(JARVIS_OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(JARVIS_OUTPUT_DIR, f"enhanced_{time.strftime('%Y%m%d_%H%M%S')}.png")
        img.save(out_path)
        os.startfile(out_path)
        return params.get("description", "Enhancement applied.")
    except Exception as e:
        return f"Pillow enhancement failed: {e}"

def analyze_input_image(prompt):
    """
    Find newest image in jarvis_input/, analyze or enhance it, delete source.
    If prompt implies enhancement, Claude returns enhancement params as JSON
    and Pillow applies them; enhanced image is saved to jarvis_output/.
    """
    os.makedirs(JARVIS_INPUT_DIR, exist_ok=True)
    exts = ["*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"]
    files = []
    for ext in exts:
        files.extend(glob.glob(os.path.join(JARVIS_INPUT_DIR, ext)))
    if not files:
        return "No image found in the jarvis_input folder."

    image_path = max(files, key=os.path.getmtime)
    print(f"  Processing image: {image_path}")

    b64, media_type = image_to_base64(image_path)

    # Detect if user wants enhancement vs plain analysis
    enhance_keywords = ["enhance", "improve", "fix", "sharpen", "brighten",
                        "denoise", "clean up", "make better", "increase contrast"]
    wants_enhancement = any(kw in (prompt or "").lower() for kw in enhance_keywords)

    if wants_enhancement:
        # Use realesrgan-ncnn-vulkan exe (runs on integrated GPU via Vulkan — no NVIDIA needed)
        # Download from: https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases
        ESRGAN_EXE = r"C:\Users\paten\OneDrive\Desktop\Tools\realesrgan-ncnn-vulkan.exe"  # ← update this path

        if not os.path.exists(ESRGAN_EXE):
            # Graceful fallback to Pillow if exe not found
            result = _pillow_enhance(image_path, prompt)
        else:
            os.makedirs(JARVIS_OUTPUT_DIR, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            out_path  = os.path.join(JARVIS_OUTPUT_DIR, f"upscaled_{timestamp}.png")
            speak("Upscaling image, this may take a moment.")
            try:
                subprocess.run(
                    [ESRGAN_EXE, "-i", image_path, "-o", out_path, "-s", "4", "-n", "realesrgan-x4plus"],
                    check=True, timeout=120
                )
                os.startfile(out_path)
                result = "Image upscaled 4x and saved to jarvis_output."
            except subprocess.TimeoutExpired:
                result = "Upscaling timed out. Try a smaller image."
            except subprocess.CalledProcessError as e:
                result = f"Upscaling failed: {e}"
            except Exception as e:
                result = f"Upscaling error: {e}"

        with _chat_lock:
            CHAT_HISTORY.append({"role": "user", "content": f"[Image enhancement request] {prompt}"})
            CHAT_HISTORY.append({"role": "assistant", "content": f"[Enhancement result] {result}"})
    else:
        # Plain analysis
        result = analyze_image_with_claude(image_path, prompt or "Describe this image.")

    # Auto-delete source image
    try:
        os.remove(image_path)
        print(f"  Deleted source image: {image_path}")
    except Exception as e:
        print(f"  Could not delete image: {e}")

    return result


# ── Web Search ────────────────────────────────────────────────────────────────
def brave_search(query, count=5):
    """Call Brave Search API and return a list of {title, description, url} dicts."""
    if not BRAVE_API_KEY:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
            params={"q": query, "count": count},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        return [{"title": r.get("title", ""), "description": r.get("description", ""), "url": r.get("url", "")} for r in results]
    except Exception as e:
        print(f"  Brave search error: {e}")
        return []

def synthesize_search_answer(query, results):
    """Ask Claude to turn raw search snippets into a short spoken answer."""
    if not results:
        return "I couldn't find anything for that. Try again or check your Brave API key."
    snippets = "\n".join(
        f"{i+1}. {r['title']}: {r['description']} ({r['url']})"
        for i, r in enumerate(results)
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="You are a voice assistant. Using the search results below, give a concise spoken answer (2-4 sentences). No markdown, no bullet points — plain conversational sentences only.",
            messages=[{"role": "user", "content": f"Question: {query}\n\nSearch results:\n{snippets}"}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"Search succeeded but summary failed: {e}"

# ── Execute Action ─────────────────────────────────────────────────────────────
def _generate_file_content(prompt_text, filename, context=""):
    """Ask Claude to write the raw file content, optionally grounded in real-time search context."""
    ext = os.path.splitext(filename)[1].lower()
    lang_hint = {
        '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript',
        '.html': 'HTML', '.css': 'CSS', '.md': 'Markdown',
        '.json': 'JSON', '.sh': 'Shell script', '.txt': 'plain text',
    }.get(ext, 'plain text')
    user_content = prompt_text
    if context:
        user_content = f"Real-time web data:\n{context}\n\nTask: {prompt_text}"
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            system=(
                f"You are a file generator. Output ONLY the raw file content with no "
                f"explanation, preamble, or markdown code fences. "
                f"The file is named '{filename}' and should be {lang_hint}. "
                f"Provide thorough, in-depth content using any real-time data supplied."
            ),
            messages=[{"role": "user", "content": user_content}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"# File generation failed: {e}"

def _summarize_file_for_speech(content, filename):
    """Generate a short 1-2 sentence spoken summary of a generated file."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            system="You are a voice assistant. In 1-2 spoken sentences, briefly summarize what was written. Be concise and conversational. No markdown or bullet points.",
            messages=[{"role": "user", "content": f"Summarize what is in '{filename}':\n\n{content[:2500]}"}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return f"{filename} is ready."

def _repair_json_strings(raw):
    """Escape literal newlines/tabs inside JSON string values so json.loads won't fail."""
    result = []
    in_string = False
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == '\\' and in_string:
            result.append(c)
            i += 1
            if i < len(raw):
                result.append(raw[i])
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            result.append(c)
        elif in_string and c == '\n':
            result.append('\\n')
        elif in_string and c == '\r':
            result.append('\\r')
        elif in_string and c == '\t':
            result.append('\\t')
        else:
            result.append(c)
        i += 1
    return ''.join(result)


def _generate_coding_skeleton(prompt_text, context=""):
    """Ask Claude Haiku to generate a project skeleton as JSON."""
    user_content = prompt_text
    if context:
        user_content = f"Real-time web data:\n{context}\n\nTask: {prompt_text}"
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=(
                'You are a project scaffolding assistant. Output ONLY a raw JSON object with this exact structure:\n'
                '{"project_name":"<short_snake_case_name>","files":[{"path":"<relative_path>","content":"<file_content>"}],"summary":"<1 spoken sentence>"}\n'
                'CRITICAL JSON RULES: All string values must use \\n for newlines, \\t for tabs, \\\\ for backslashes, \\" for quotes. Never use literal newlines inside string values.\n'
                'Framework preference: If the project is a web application, website, frontend, UI, dashboard, or anything browser-based, scaffold it as a React app using Vite (npm create vite). Include src/App.jsx, src/main.jsx, index.html, package.json with react + vite devDependencies, and an install.bat running "npm install". Do NOT use plain HTML/CSS/JS for web projects.\n'
                'Always include:\n'
                '- A main entry point file with skeleton code and TODO comments marking what needs implementing\n'
                '- A requirements.txt (Python) or package.json (JS/TS) listing all needed dependencies\n'
                '- An install.bat that installs all dependencies in one command (e.g. pip install -r requirements.txt or npm install)\n'
                '- A CLAUDE.md describing the project goal, file structure, and what still needs to be implemented\n'
                '- A .gitignore appropriate for the project type. Always exclude: .env, .env.*, *.env, .vscode/, .idea/, *.log, *.tmp. For Python projects also exclude: __pycache__/, *.pyc, *.pyo, venv/, .venv/, dist/, *.egg-info/. For Node/JS projects also exclude: node_modules/, dist/, .next/, .cache/, coverage/.\n'
                'Keep file content minimal — stubs with clear TODO comments, not full implementations.\n'
                'No markdown fences. Output raw JSON only.'
            ),
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[^\n]*\n", "", raw)
            raw = re.sub(r"\n```$", "", raw.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return json.loads(_repair_json_strings(raw))
    except Exception as e:
        print(f"  Skeleton generation error: {e}")
        return None

def _push_file_to_dashboard(name, content):
    entry = {"name": name, "content": content,
             "time": time.strftime("%H:%M:%S"), "size": f"{len(content):,} chars"}
    with _dash_lock:
        _dash_state["files"].append(entry)

def _push_state(state, transcript=""):
    with _dash_lock:
        _dash_state["state"] = state
        if transcript:
            _dash_state["transcript"] = transcript

# ── Word-beat sync ────────────────────────────────────────────────────────────
_beat_stop = threading.Event()

def _start_word_beats(text):
    _beat_stop.clear()
    words = len(text.split())
    wps   = max(1.0, (130 + SPEECH_RATE * 5) / 60)

    def _run():
        # PowerShell SAPI needs ~0.7 s to initialize before audio starts.
        # wait() returns True if stopped early (interrupted), False on timeout.
        if _beat_stop.wait(timeout=0.72):
            return
        _push_state("speaking")
        for _ in range(words):
            if _beat_stop.is_set():
                break
            with _dash_lock:
                _dash_state["speech_beat"] += 1
            _beat_stop.wait(timeout=1.0 / wps)

    threading.Thread(target=_run, daemon=True).start()

def _stop_word_beats():
    _beat_stop.set()

def execute_action(action, workspaces, activated_event=None):
    kind   = action.get("type", "")
    target = action.get("target", "")
    query  = action.get("query", "")

    if kind == "workspace":
        ok, msg = open_workspace(target, workspaces)
        return msg

    elif kind == "url":
        webbrowser.open(target)
        return f"Opened {target}"

    elif kind == "search":
        engine = action.get("engine", "google")
        urls = {
            "google":  f"https://google.com/search?q={query.replace(' ', '+')}",
            "youtube": f"https://youtube.com/results?search_query={query.replace(' ', '+')}",
            "github":  f"https://github.com/search?q={query.replace(' ', '+')}",
        }
        webbrowser.open(urls.get(engine, urls["google"]))
        return f"Searched {engine} for '{query}'"

    elif kind == "app":
        open_app(target)
        return f"Launched {target}"

    elif kind == "vscode":
        subprocess.Popen(["code", target] if target else ["code"], shell=True)
        return f"Opened VSCode{' at ' + target if target else ''}"

    elif kind == "file":
        if os.path.exists(target):
            os.startfile(target)
            return f"Opened {target}"
        matches = fuzzy_match_files(target, FILE_INDEX)
        if not matches:
            return f"File not found: {target}"
        if len(matches) == 1:
            os.startfile(matches[0])
            return f"Opened {matches[0]}"
        results_path = os.path.join(JARVIS_OUTPUT_DIR, "jarvis_matches.txt")
        os.makedirs(JARVIS_OUTPUT_DIR, exist_ok=True)
        with open(results_path, "w") as f:
            for i, p in enumerate(matches[:20], 1):
                f.write(f"{i}. {p}\n")
        os.startfile(results_path)
        speak(f"I found {min(len(matches), 20)} matches. I've opened a list. Say the numbers you want opened.")
        nums = listen_for_selection(activated_event)
        if not nums:
            return "Selection cancelled."
        opened = [n for n in nums if 1 <= n <= len(matches)]
        for n in opened:
            os.startfile(matches[n-1])
            time.sleep(0.3)
        return f"Opened {len(opened)} file(s): {', '.join(os.path.basename(matches[n-1]) for n in opened)}"

    elif kind == "find_files":
        keyword = action.get("keyword", target).lower()
        matches = fuzzy_match_files(keyword, FILE_INDEX, threshold=60, limit=20)
        if not matches:
            return f"No files found matching '{keyword}'"
        if len(matches) == 1:
            os.startfile(matches[0])
            return f"Opened {matches[0]}"
        results_path = os.path.join(JARVIS_OUTPUT_DIR, "jarvis_matches.txt")
        os.makedirs(JARVIS_OUTPUT_DIR, exist_ok=True)
        with open(results_path, "w") as f:
            for i, p in enumerate(matches, 1):
                f.write(f"{i}. {p}\n")
        os.startfile(results_path)
        speak(f"Found {len(matches)} files. List is open. Say the numbers you want opened.")
        nums = listen_for_selection(activated_event)
        if not nums:
            return "Selection cancelled."
        opened = [n for n in nums if 1 <= n <= len(matches)]
        for n in opened:
            os.startfile(matches[n-1])
            time.sleep(0.3)
        return f"Opened {len(opened)} file(s): {', '.join(os.path.basename(matches[n-1]) for n in opened)}"

    elif kind == "bookmark":
        keyword = target.lower()
        matches = [b for b in BOOKMARKS if keyword in b["name"].lower()]
        if matches:
            webbrowser.open(matches[0]["url"])
            return f"Opened bookmark: {matches[0]['name']}"
        return f"No bookmark found matching '{target}'"

    elif kind == "history":
        keyword  = action.get("keyword", target)
        days_ago = action.get("days_ago", None)
        results  = search_history(keyword=keyword, days_ago=days_ago)
        if results:
            print("\n  ── History matches ──────────────────")
            for i, e in enumerate(results):
                print(f"  {i+1}. [{e['visited']}] {e['title']}\n     {e['url']}")
            print("  ─────────────────────────────────────\n")
            webbrowser.open(results[0]["url"])
            reply = f"Found {len(results)} match. Opening: {results[0]['title'] or results[0]['url']}"
            speak(reply)
            return reply
        return f"No history found matching '{keyword}'"

    elif kind == "screenshot":
        prompt = action.get("prompt", "")
        speak("Taking a screenshot.")
        result = take_screenshot(prompt)
        print(f"\n  ── Screenshot Analysis ──────────────\n  {result}\n  ────────────────────────────────────\n")
        speak(result)
        return result

    elif kind == "analyze_image":
        prompt = action.get("prompt", "")
        wants_enhancement = any(
            kw in (prompt or "").lower()
            for kw in ["enhance", "improve", "fix", "sharpen", "brighten",
                       "denoise", "clean up", "make better", "increase contrast", "upscale"]
        )

        if wants_enhancement:
            # Run in background thread — Jarvis stays responsive
            def _enhance_bg():
                result = analyze_input_image(prompt)
                speak(result)  # speak() is thread-safe

            threading.Thread(target=_enhance_bg, daemon=True).start()
            speak("Enhancement started in the background. I'll let you know when it's done.")
            return "Enhancement running in background."
        else:
            # Plain analysis is fast — run inline as before
            speak("Analyzing image.")
            result = analyze_input_image(prompt)
            print(f"\n  ── Image Analysis ───────────────────\n  {result}\n  ────────────────────────────────────\n")
            speak(result)
            return result

    elif kind == "web_search":
        if not BRAVE_API_KEY:
            msg = "Web search isn't set up. Add BRAVE_API_KEY to your .env file."
            speak(msg)
            return msg
        search_query = action.get("query", target)
        speak("Let me look that up.")
        results = brave_search(search_query)
        answer = synthesize_search_answer(search_query, results)
        print(f"\n  ── Web Search: {search_query} ──────────\n  {answer}\n  ────────────────────────────────────\n")
        speak(answer)
        with _chat_lock:
            CHAT_HISTORY.append({"role": "user", "content": f"[Web search] {search_query}"})
            CHAT_HISTORY.append({"role": "assistant", "content": answer})
        return f"Web search: {search_query}"

    elif kind == "generate_file":
        filename     = action.get("filename", "jarvis_output.txt")
        prompt_text  = action.get("prompt", "")
        search_query = action.get("search_query", "")
        speak(f"Generating {filename}.")
        context = ""
        if search_query and BRAVE_API_KEY:
            speak("Looking up current data first.")
            results = brave_search(search_query, count=6)
            if results:
                context = "\n".join(
                    f"{r['title']}: {r['description']} ({r['url']})" for r in results
                )
        elif search_query and not BRAVE_API_KEY:
            speak("Note: no Brave API key, so current data unavailable.")
        content = _generate_file_content(prompt_text, filename, context)
        os.makedirs(JARVIS_OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(JARVIS_OUTPUT_DIR, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
        _push_file_to_dashboard(filename, content)
        summary = _summarize_file_for_speech(content, filename)
        speak(summary)
        return f"Generated {filename}"

    elif kind == "coding_mode":
        prompt_text = action.get("prompt", "")
        speak("Starting coding mode. Generating project skeleton.")
        context = ""
        if BRAVE_API_KEY:
            results = brave_search(prompt_text[:200], count=4)
            if results:
                context = "\n".join(
                    f"{r['title']}: {r['description']}" for r in results
                )
        skeleton = _generate_coding_skeleton(prompt_text, context)
        if not skeleton:
            speak("Skeleton generation failed. Try again.")
            return "Coding mode: generation failed"
        project_name = re.sub(r'[^\w-]', '_', skeleton.get("project_name", "project")).strip('_') or "project"
        project_dir  = os.path.join(CODING_DIR, project_name)
        os.makedirs(project_dir, exist_ok=True)
        for f in skeleton.get("files", []):
            rel = f.get("path", "").lstrip("/\\")
            if not rel:
                continue
            full_path = os.path.join(project_dir, rel)
            parent = os.path.dirname(full_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(f.get("content", ""))
            _push_file_to_dashboard(rel, f.get("content", ""))
        subprocess.Popen(["code", project_dir], shell=True)
        subprocess.Popen(
            f'start "Claude Code – {project_name}" cmd /k "cd /d "{project_dir}" && claude"',
            shell=True
        )
        summary = skeleton.get("summary", f"Project {project_name} is ready.")
        speak(summary)
        return f"Coding mode: {project_name} at {project_dir}"

    elif kind == "none":
        return "No action"

    return f"Unknown action: {kind}"

# ── Jarvis Brain ───────────────────────────────────────────────────────────────
CHAT_HISTORY = []
_chat_lock = threading.Lock()

def build_system_prompt(workspaces):
    workspace_list = json.dumps(list(workspaces.keys()))

    return f"""You are JARVIS. Return ONLY a single JSON object, no explanation.

WORKSPACES (use type "workspace"): {workspace_list}
BOOKMARKS searchable by keyword (use type "bookmark").
FILES searchable by keyword (use type "find_files").
HISTORY searchable by keyword (use type "history").

ACTION TYPES (pick one):
{{"mode":"action","actions":[{{"type":"workspace","target":"<n>"}}]}}
{{"mode":"action","actions":[{{"type":"url","target":"<full url>"}}]}}
{{"mode":"action","actions":[{{"type":"app","target":"<app name>"}}]}}
{{"mode":"action","actions":[{{"type":"search","engine":"google|youtube|github","query":"<q>"}}]}}
{{"mode":"action","actions":[{{"type":"vscode","target":"<path>"}}]}}
{{"mode":"action","actions":[{{"type":"find_files","keyword":"<word>","extension":"<or empty>"}}]}}
{{"mode":"action","actions":[{{"type":"bookmark","target":"<keyword>"}}]}}
{{"mode":"action","actions":[{{"type":"history","keyword":"<word>","days_ago":null}}]}}
{{"mode":"action","actions":[{{"type":"screenshot","prompt":"<what to analyze or do with the screenshot>"}}]}}
{{"mode":"action","actions":[{{"type":"analyze_image","prompt":"<what to do with the image in the input folder>"}}]}}
{{"mode":"action","actions":[{{"type":"web_search","query":"<search query>"}}]}}
{{"mode":"action","actions":[{{"type":"generate_file","filename":"<name.ext>","prompt":"<full description of what to write in the file>","search_query":"<targeted web search query, or empty string if no current data needed>"}}]}}
{{"mode":"action","actions":[{{"type":"coding_mode","prompt":"<full description of the project/feature to scaffold>"}}]}}
{{"mode":"chat","reply":"<your answer>"}}
{{"mode":"none"}}

RULES:
- "open [app]" → type "app"
- "open [bookmark keyword]" → type "bookmark"
- "find files" or "open file" → type "find_files" with keyword
- "open [workspace]" → type "workspace". Fuzzy match (Example: "311","three eleven","3-11" all match workspace "311")
- Words like "open","find","search","launch","show" → ALWAYS mode "action"
- "screenshot","take a screenshot","capture screen" → type "screenshot"; put intent in "prompt"
- "analyze image","look at this","what's in the image", "enhance image" → type "analyze_image"; put intent in "prompt"
- Anything needing current/real-time info WITHOUT file generation: news, weather, sports scores, prices, recent events → type "web_search"
- "write a [file]", "create a [file]", "generate [file]", "make a [file]" → type "generate_file"; filename must include an extension (.py, .txt, .md, .html, etc.); put the full description of what to write in "prompt"; if the file content requires current/real-time data (e.g. today's news, current prices, recent stats, live standings), set "search_query" to a targeted search query — otherwise leave it as an empty string ""
- "code [thing]", "coding mode [thing]", "build a project for [thing]", "start a project", "scaffold [thing]" → type "coding_mode"; put the full description in "prompt"
- Any question or request for information that doesn't need real-time data → mode "chat" with a concise spoken reply
- Unclear/filler → mode "none"
- Keep chat replies SHORT: 1-2 sentences max. No markdown, no lists. Plain spoken sentences only. Answer only what was asked — no extra context unless the user asks to go in depth.
"""

_ai_error_count = 0
_AI_ERROR_RESET_THRESHOLD = 3

def init_chat_history(workspaces):
    global CHAT_HISTORY
    system_msg = build_system_prompt(workspaces)
    CHAT_HISTORY = [{"role": "system", "content": system_msg}]

def init_claude(workspaces):
    init_chat_history(workspaces)
    speak("Ready for your command.")

# ── Pre-AI Filter ─────────────────────────────────────────────────────────────
BYPASS_COMMANDS = {
    "never mind", "nevermind", "forget it", "forget that", "cancel", "stop",
    "nothing", "nope", "no", "abort", "disregard", "ignore that",
    "never mind that", "scratch that", "skip it",
    "um", "uh", "hmm", "hm", "okay", "ok", "yeah", "yes", "alright",
    "thanks", "thank you", "cool", "got it",
}

def should_bypass_ai(text):
    cleaned = text.strip().lower().rstrip(".,!?")
    return cleaned in BYPASS_COMMANDS

def ask_claude(command, workspaces):
    global CHAT_HISTORY, _ai_error_count

    with _chat_lock:
        history_snapshot = CHAT_HISTORY.copy()
        CHAT_HISTORY.append({"role": "user", "content": command})
        messages = [m for m in CHAT_HISTORY if m["role"] != "system"]
        system_prompt = next((m["content"] for m in CHAT_HISTORY if m["role"] == "system"), "")

    try:
        # chat mode needs more tokens for a full reply; action mode needs very few
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system_prompt,
            messages=messages
        )

        raw = response.content[0].text.strip()

        # Strip markdown fences if model adds them
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.split("```")[0]

        def try_parse(text):
            text = text.replace("\\ ", " ").replace("\\.", ".")
            try:
                return json.loads(text)
            except Exception:
                pass
            try:
                start = text.find("{")
                if start != -1:
                    depth = 0
                    for i, c in enumerate(text[start:], start):
                        if c == "{": depth += 1
                        elif c == "}":
                            depth -= 1
                            if depth == 0:
                                return json.loads(text[start:i+1])
            except Exception:
                pass
            try:
                start = text.find("{")
                chunk = text[start:]
                for end in range(len(chunk), 0, -1):
                    try:
                        return json.loads(chunk[:end])
                    except Exception:
                        continue
            except Exception:
                pass
            return None

        result = try_parse(raw.strip())

        with _chat_lock:
            if result:
                _ai_error_count = 0
                if len(CHAT_HISTORY) > 13:
                    CHAT_HISTORY = CHAT_HISTORY[:1] + CHAT_HISTORY[-12:]
                CHAT_HISTORY.append({"role": "assistant", "content": raw})
                return result

            _ai_error_count += 1
            CHAT_HISTORY = history_snapshot
            if _ai_error_count >= _AI_ERROR_RESET_THRESHOLD:
                print(f"  {_ai_error_count} consecutive AI errors — resetting chat history.")
                init_chat_history(workspaces)
                _ai_error_count = 0
        print(f"  Could not parse AI response: {raw[:100]}")
        return {"mode": "none"}

    except Exception as e:
        with _chat_lock:
            CHAT_HISTORY = history_snapshot
        print(f"  Unexpected AI error: {e}")
        return {"mode": "none"}

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

def ask_claude_async(command, workspaces):
    future = _executor.submit(ask_claude, command, workspaces)
    return future

# ── Wake Word Detection ─────────────────────────────────────────────────────────
WAKE_WORD = "jarvis"
# Rolling noise-floor estimate in int16 RMS units (0–32767).  Adapts to the
# room automatically; threshold = max(ambient * 4.0, 100).
_ambient_rms = [200.0]
q = _queue.Queue()

model = vosk.Model(os.path.join(os.path.dirname(__file__), "models", "vosk-model-small-en-us-0.15"))

def callback(indata, frames, time, status):
    q.put(bytes(indata))

def listen_for_wake_word(activated_event):
    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16',
                           channels=1, callback=callback):
        rec = vosk.KaldiRecognizer(model, 16000)
        while True:
            data = q.get()
            arr = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            threshold = max(_ambient_rms[0] * 4.0, 100.0)
            if rms < threshold:
                # quiet chunk — slowly pull the noise floor toward current level
                _ambient_rms[0] = 0.95 * _ambient_rms[0] + 0.05 * rms
                continue
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text = result.get("text", "")
                if WAKE_WORD in text.lower():
                    print("Wake word detected!")
                    stop_tts()
                    activated_event.set()
                    return

# ── Voice Recording For File Selection ──────────────────────────────────────────
def listen_for_selection(activated_event):
    speak("Let me know when you're ready to select")
    activated_event.wait(timeout=60)
    activated_event.clear()
    speak("Which numbers?")
    _tts_queue.join()
    selection = listen_for_command(max_duration=20, silence_duration=3.0)
    if not selection or "cancel" in selection.lower():
        speak("Cancelled.")
        return []
    nums = [int(n) for n in re.findall(r'\b(\d+)\b', selection) if 1 <= int(n) <= 20]
    word_map = {"one":1,"two":2,"three":3,"four":4,"five":5,
                "six":6,"seven":7,"eight":8,"nine":9,"ten":10}
    for word, num in word_map.items():
        if word in selection.lower():
            nums.append(num)
    nums = sorted(set(nums))
    if not nums:
        speak("No valid numbers heard. Cancelling.")
    return nums

# ── Persistent audio stream ───────────────────────────────────────────────────
_audio_buffer = []
_audio_lock = threading.Lock()
_capture_active = threading.Event()
_capture_done = threading.Event()

def _persistent_audio_callback(indata, frames, time_info, status):
    if not _capture_active.is_set():
        return
    with _audio_lock:
        _audio_buffer.append(indata.copy())


_persistent_stream = None

def start_persistent_stream():
    global _persistent_stream
    _persistent_stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1,
        blocksize=CHUNK, dtype="float32",
        callback=_persistent_audio_callback
    )
    _persistent_stream.start()

def listen_for_command(max_duration=8):
    """Record a command using WebRTC VAD for end-of-speech, then verify speaker identity."""
    time.sleep(0.05)

    # 20 ms frames at 16 kHz → 50 frames/sec
    SPEECH_ONSET = 4   # consecutive speech frames to confirm speech started (~80 ms)
    SILENCE_END  = 40  # consecutive silence frames to stop recording (~800 ms)
    MAX_FRAMES   = max_duration * 50

    vad = _webrtcvad.Vad(3) if _WEBRTCVAD_OK else None

    with _audio_lock:
        _audio_buffer.clear()
    _capture_active.set()

    print("  Speak your command...")

    all_chunks      = []
    consec_speech   = 0
    consec_silence  = 0
    speaking_started = False
    speech_start_idx = 0
    done = False
    deadline = time.monotonic() + max_duration + 1.0

    while len(all_chunks) < MAX_FRAMES and not done:
        if time.monotonic() > deadline:
            break
        with _audio_lock:
            new_chunks = list(_audio_buffer)
            _audio_buffer.clear()

        if not new_chunks:
            time.sleep(0.01)
            continue

        for chunk in new_chunks:
            all_chunks.append(chunk)

            if vad is not None:
                pcm = np.clip(chunk.flatten() * 32767, -32768, 32767).astype(np.int16)
                try:
                    is_speech = vad.is_speech(pcm.tobytes(), SAMPLE_RATE)
                except Exception:
                    is_speech = True
            else:
                # Fallback: simple amplitude gate
                is_speech = float(np.abs(chunk).max()) > 0.02

            if is_speech:
                consec_speech  += 1
                consec_silence  = 0
                if not speaking_started and consec_speech >= SPEECH_ONSET:
                    speaking_started = True
                    # Include a couple of frames before the confirmed onset
                    speech_start_idx = max(0, len(all_chunks) - SPEECH_ONSET - 2)
            else:
                consec_speech = 0
                if speaking_started:
                    consec_silence += 1

            if speaking_started and consec_silence >= SILENCE_END:
                done = True
                break

    _capture_active.clear()

    if not speaking_started:
        print("  No speech detected.")
        return ""

    # Trim trailing silence from the recording
    speech_end = len(all_chunks) - consec_silence
    recording   = np.concatenate(all_chunks[speech_start_idx:speech_end], axis=0)

    tmp_path = os.path.join(tempfile.gettempdir(), "_jarvis_tmp.wav")
    sf.write(tmp_path, recording, SAMPLE_RATE)

    print("  Processing speech...")
    try:
        with _whisper_lock:
            segments, _ = WHISPER_MODEL.transcribe(
                tmp_path,
                language="en",
                vad_filter=True,
                initial_prompt="Open Discord, search YouTube for, open workspace, never mind, stop, yes, no, cancel, open file, find files, take a screenshot, analyze image, what's on screen",
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    speech_pad_ms=200,
                ))
        text = " ".join(s.text for s in segments).strip()
        if text:
            print(f'  Heard: "{text}"')
        else:
            print("  Could not understand audio.")
            return ""
    except Exception as e:
        print(f"  Whisper error: {e}")
        return ""
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    return text

# ── Handle AI Response ─────────────────────────────────────────────────────────
def handle_response(response, workspaces, activated_event=None):
    # Normalise bare action dicts (model sometimes skips the wrapper)
    if response.get("mode") in ("find_files", "file", "bookmark", "history", "url",
                                 "app", "search", "workspace", "vscode",
                                 "screenshot", "analyze_image", "web_search",
                                 "coding_mode"):
        response = {"mode": "action", "actions": [response]}

    mode = response.get("mode", "none")

    if mode == "action":
        actions = response.get("actions", [])
        actions = [a for a in actions if isinstance(a, dict)]
        if not actions:
            speak("I'm not sure what to do with that.")
            return
        for action in actions:
            result = execute_action(action, workspaces, activated_event)
            print(f"  Done: {result}")

    elif mode == "chat":
        # General AI answer — speak it and store in history (already done in ask_claude)
        reply = response.get("reply", "I'm not sure how to answer that.")
        print(f"\n  ── JARVIS ──────────────────────────\n  {reply}\n  ────────────────────────────────────\n")
        speak(reply)

    elif mode == "none":
        print("  No action taken.")

    else:
        print(f"  Unknown mode: {mode}")

# ── Main ───────────────────────────────────────────────────────────────────────
LISTENING_FOR_ACTIVATION = True

def main():
    workspaces = load_workspaces()

    # Ensure I/O folders exist
    os.makedirs(JARVIS_INPUT_DIR, exist_ok=True)
    os.makedirs(JARVIS_OUTPUT_DIR, exist_ok=True)

    # Detect Tailscale and obtain HTTPS cert before starting server
    global _TAILSCALE_CERT_PATH, _TAILSCALE_KEY_PATH, _tailscale_url
    _ts_ip, _ts_fqdn = _detect_tailscale()
    if _ts_fqdn:
        print("  Tailscale detected — requesting TLS cert (this may take a moment)...")
        _TAILSCALE_CERT_PATH, _TAILSCALE_KEY_PATH = _setup_tailscale_https(_ts_fqdn)
        if _TAILSCALE_CERT_PATH:
            _tailscale_url = f"https://{_ts_fqdn}:5151/phone"
        else:
            _tailscale_url = f"http://{_ts_ip}:5151/phone"
    elif _ts_ip:
        _tailscale_url = f"http://{_ts_ip}:5151/phone"

    # Start embedded dashboard server
    threading.Thread(target=_start_embedded_server, daemon=True).start()
    time.sleep(0.6)   # give Flask a moment to bind before opening browser
    if _TAILSCALE_CERT_PATH and _ts_fqdn:
        webbrowser.open(f"https://{_ts_fqdn}:5151")
    else:
        webbrowser.open("http://localhost:5151")

    # Build indexes in background so startup isn't slow
    threading.Thread(target=build_file_index,     daemon=True).start()
    threading.Thread(target=build_bookmark_index,  daemon=True).start()
    threading.Thread(target=build_history_index,   daemon=True).start()

    init_claude(workspaces)
    start_persistent_stream()


    print(f"\n{'='*50}")
    print("  JARVIS is ready")
    _dash_url = f"https://{_ts_fqdn}:5151" if _TAILSCALE_CERT_PATH and _ts_fqdn else "http://localhost:5151"
    print(f"  Dashboard:  {_dash_url}")
    if _tailscale_url:
        scheme = "https" if _TAILSCALE_CERT_PATH else "http"
        tls_note = "" if _TAILSCALE_CERT_PATH else " (no HTTPS — mic may be blocked; run 'tailscale cert' manually)"
        print(f"  Phone mic:  {_tailscale_url}{tls_note}")
    else:
        print(f"  Phone mic:  Tailscale not detected — install Tailscale for remote phone access")
    print(f"  Workspaces: {list(workspaces.keys()) or 'none'}")
    print(f"  AI: Claude Haiku 4.5")
    print(f"  OS: {OS}")
    print(f"  Input folder:  {JARVIS_INPUT_DIR}")
    print(f"  Output folder: {JARVIS_OUTPUT_DIR}")
    print(f"{'='*50}")
    print("\n  Say 'Jarvis' to activate...\n")
    print("  Tip: Say 'take a screenshot' to capture and analyze the screen.")
    print("  Tip: Drop an image in jarvis_input/ then say 'analyze image'.")
    print("  Tip: Say 'generate [filename]' to create a file with current data support.\n")

    activated_event = threading.Event()

    def voice_thread():
        global LISTENING_FOR_ACTIVATION
        while True:
            if LISTENING_FOR_ACTIVATION and not activated_event.is_set():
                listen_for_wake_word(activated_event)
            else:
                time.sleep(0.02)

    threading.Thread(target=voice_thread, daemon=True).start()

    while True:
        try:
            print("Waiting for wake-word or phone command...")
            LISTENING_FOR_ACTIVATION = True
            phone_command = None
            while not activated_event.wait(timeout=0.05):
                if not _phone_command_queue.empty():
                    phone_command = _phone_command_queue.get_nowait()
                    break
            LISTENING_FOR_ACTIVATION = False
            activated_event.clear()
            _tts_suppressed.clear()

            if phone_command:
                print(f'\n  Phone command: "{phone_command}"')
                command = phone_command
            else:
                _push_state("activated")
                print("\n  Activated!")
                speak("Yes sir.")
                _tts_queue.join()
                _push_state("activated")

                command = listen_for_command()
                if not command:
                    print("  No command heard. Say 'Jarvis' again.\n")
                    speak("I didn't catch that. Try again.")
                    _tts_queue.join()
                    _push_state("idle")
                    continue

            print("  Thinking...")
            _push_state("thinking", transcript=command)
            if should_bypass_ai(command):
                print("  Bypassed AI (dismissal command).")
                _push_state("idle")
                continue

            future = ask_claude_async(command, workspaces)
            try:
                response = future.result(timeout=15)
            except Exception as e:
                print(f"  AI error: {e}")
                speak("Something went wrong. Please try again.")
                _tts_queue.join()
                _push_state("error")
                continue

            # Re-enable wake word so user can interrupt Jarvis mid-response
            LISTENING_FOR_ACTIVATION = True
            try:
                handle_response(response, workspaces, activated_event)
            except Exception as e:
                print(f"  Error in handle_response: {e}")
            _tts_queue.join()
            _push_state("idle")
            print("\n  Say 'Jarvis' to activate...\n")

        except KeyboardInterrupt:
            speak("Shutting down. Goodbye.")
            _tts_queue.join()
            print("\n  JARVIS shutting down. Goodbye.")
            break
        except Exception as e:
            print(f"  Unexpected main loop error: {e}")
            _push_state("idle")
            time.sleep(0.5)


if __name__ == "__main__":
    main()