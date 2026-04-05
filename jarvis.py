#!/usr/bin/env python3
"""
JARVIS - Voice-Activated AI Assistant
Clap twice -> speak your command -> local AI figures out what to do
"""

import os
import sys
import json
import time
import subprocess
import webbrowser
import platform
import requests
import threading
import pyttsx3

try:
    import sounddevice as sd
    import numpy as np
    from faster_whisper import WhisperModel
    import soundfile as sf
except ImportError:
    print("Missing dependencies. Run: pip install sounddevice numpy faster-whisper soundfile")
    sys.exit(1)

print("  Loading Whisper model (first run may take a moment)...")
WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")

# ── Voice Engine ─────────────────────────────────────────────────────────────
_tts_engine = pyttsx3.init()
_tts_engine.setProperty("rate", 175)    # speed (words per minute)
_tts_engine.setProperty("volume", 1.0)  # 0.0 to 1.0
# Pick a voice — 0 = first available (usually male), 1 = second (usually female)
voices = _tts_engine.getProperty("voices")
if len(voices) > 1:
    _tts_engine.setProperty("voice", voices[0].id)  # change to voices[0] for male
_tts_lock = threading.Lock()

def speak(text):
    """Speak text without blocking the main thread."""
    def _speak():
        with _tts_lock:
            _tts_engine.say(text)
            _tts_engine.runAndWait()
    threading.Thread(target=_speak, daemon=True).start()

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACES_FILE = os.path.join(os.path.dirname(__file__), "workspaces.json")
CLAP_THRESHOLD  = 0.3
CLAP_GAP_MIN    = 0.15
CLAP_GAP_MAX    = 1.2
SAMPLE_RATE     = 44100
CHUNK           = 1024
OS              = platform.system()

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
            if OS == "Windows":
                subprocess.Popen(["code", path], shell=True)
            elif OS == "Darwin":
                subprocess.Popen(["open", "-a", "Visual Studio Code", path])
            else:
                subprocess.Popen(["code", path])
            opened.append(f"VSCode: {path}")
        elif kind == "file":
            if OS == "Windows":
                os.startfile(path)
            elif OS == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            opened.append(f"File: {path}")
        elif kind == "app":
            if OS == "Windows":
                subprocess.Popen(["start", path], shell=True)
            elif OS == "Darwin":
                subprocess.Popen(["open", "-a", path])
            else:
                subprocess.Popen([path])
            opened.append(f"App: {path}")

        time.sleep(0.3)

    return True, f"Opened workspace '{name}': {', '.join(opened)}"

# ── System Actions ─────────────────────────────────────────────────────────────
def execute_action(action, workspaces):
    kind   = action.get("type")
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

    elif kind == "vscode":
        if OS == "Windows":
            subprocess.Popen(["code", target] if target else ["code"], shell=True)
        elif OS == "Darwin":
            subprocess.Popen(["open", "-a", "Visual Studio Code", target] if target else ["open", "-a", "Visual Studio Code"])
        else:
            subprocess.Popen(["code", target] if target else ["code"])
        return f"Opened VSCode{' at ' + target if target else ''}"

    elif kind == "app":
        if OS == "Windows":
            subprocess.Popen(["start", target], shell=True)
        elif OS == "Darwin":
            subprocess.Popen(["open", "-a", target])
        else:
            subprocess.Popen([target.lower()])
        return f"Opened {target}"

    elif kind == "file":
        if OS == "Windows":
            os.startfile(target)
        elif OS == "Darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return f"Opened file {target}"

    elif kind == "terminal":
        cmd = action.get("command", "")
        if OS == "Windows":
            subprocess.Popen(["start", "cmd", "/k", cmd], shell=True)
        elif OS == "Darwin":
            subprocess.Popen(["osascript", "-e", f'tell app "Terminal" to do script "{cmd}"'])
        else:
            subprocess.Popen(["x-terminal-emulator", "-e", cmd])
        return f"Ran terminal command: {cmd}"

    return "Unknown action"

# ── Ollama Brain ───────────────────────────────────────────────────────────────
CHAT_HISTORY = []

def init_ollama(workspaces):
    global CHAT_HISTORY
    workspace_list = json.dumps(list(workspaces.keys()))
    system_msg = (
        "You are JARVIS, a voice assistant. "
        "For every message I send, parse it as a voice command and return a JSON array of actions. "
        "Never explain. Never use markdown. Return ONLY a valid JSON array.\n\n"
        f"Available workspaces: {workspace_list}\n\n"
        "Action types:\n"
        '- {"type": "workspace", "target": "<n>"}\n'
        '- {"type": "url", "target": "<url>"}\n'
        '- {"type": "search", "engine": "google|youtube|github", "query": "<q>"}\n'
        '- {"type": "app", "target": "<app name>"}\n'
        '- {"type": "vscode", "target": "<path>"}\n'
        '- {"type": "file", "target": "<path>"}\n\n'
        "Match workspace names fuzzily."
    )
    CHAT_HISTORY = [{"role": "system", "content": system_msg}]
    print("  Warming up AI model...")
    requests.post("http://localhost:11434/api/chat", json={
        "model": "llama3.2",
        "messages": CHAT_HISTORY + [{"role": "user", "content": "open google"}],
        "stream": False
    }, timeout=60)
    print("  AI model ready!")
    speak("JARVIS online. Ready for your command.")

def ask_ollama(command, workspaces):
    global CHAT_HISTORY
    CHAT_HISTORY.append({"role": "user", "content": command})

    response = requests.post("http://localhost:11434/api/chat", json={
        "model": "llama3.2",
        "messages": CHAT_HISTORY,
        "stream": False
    }, timeout=30)

    raw = response.json()["message"]["content"].strip()

    CHAT_HISTORY.append({"role": "assistant", "content": raw})
    if len(CHAT_HISTORY) > 7:
        CHAT_HISTORY = CHAT_HISTORY[:1] + CHAT_HISTORY[-6:]

    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.split("```")[0]

    start = raw.find("[")
    end   = raw.rfind("]") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    return json.loads(raw.strip())

# ── Clap Detection ─────────────────────────────────────────────────────────────
def detect_claps():
    clap_times = []
    done = [False]
    print("  Listening for claps...")

    def callback(indata, frames, time_info, status):
        volume = float(np.abs(indata).max())
        if volume > CLAP_THRESHOLD:
            now = time.time()
            nonlocal clap_times
            clap_times = [t for t in clap_times if now - t < CLAP_GAP_MAX]
            if not clap_times or (now - clap_times[-1]) > CLAP_GAP_MIN:
                clap_times.append(now)
            if len(clap_times) >= 2:
                done[0] = True
                raise sd.CallbackStop()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        blocksize=CHUNK, callback=callback):
        while not done[0]:
            time.sleep(0.05)

    return True

# ── Voice Recording ────────────────────────────────────────────────────────────
def listen_for_command(max_duration=10, silence_threshold=0.01, silence_duration=1.2):
    print("  Speak your command... (stops when you go quiet)")
    frames = []
    silent_chunks = 0
    speaking_started = False
    silence_chunks_needed = int((SAMPLE_RATE / CHUNK) * silence_duration)
    max_chunks = int((SAMPLE_RATE / CHUNK) * max_duration)
    chunk_count = [0]
    done = [False]

    def callback(indata, frame_count, time_info, status):
        nonlocal silent_chunks, speaking_started
        volume = float(np.abs(indata).max())
        frames.append(indata.copy())
        chunk_count[0] += 1

        if volume > silence_threshold:
            speaking_started = True
            silent_chunks = 0
        elif speaking_started:
            silent_chunks += 1

        if chunk_count[0] >= max_chunks:
            done[0] = True
            raise sd.CallbackStop()
        if speaking_started and silent_chunks >= silence_chunks_needed:
            done[0] = True
            raise sd.CallbackStop()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        blocksize=CHUNK, dtype="float32", callback=callback):
        while not done[0]:
            time.sleep(0.05)

    if not frames or not speaking_started:
        print("  No speech detected.")
        return ""

    recording = np.concatenate(frames, axis=0)
    tmp_path = os.path.join(os.path.dirname(__file__), "_jarvis_tmp.wav")
    sf.write(tmp_path, recording, SAMPLE_RATE)

    print("  Processing speech...")
    try:
        segments, _ = WHISPER_MODEL.transcribe(tmp_path, language="en")
        text = " ".join(s.text for s in segments).strip()
        if text:
            print(f'  Heard: "{text}"')
        else:
            print("  Could not understand audio.")
        return text
    except Exception as e:
        print(f"  Whisper error: {e}")
        return ""
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Check Ollama is running
    try:
        requests.get("http://localhost:11434", timeout=3)
    except Exception:
        print("ERROR: Ollama is not running.")
        print("  Open the Ollama app from your Start menu, then run this script again.")
        sys.exit(1)

    workspaces = load_workspaces()
    init_ollama(workspaces)
    print(f"\n{'='*50}")
    print("  JARVIS is ready")
    print(f"  Workspaces: {list(workspaces.keys()) or 'none'}")
    print(f"  AI: Ollama llama3.2 (local)")
    print(f"  OS: {OS}")
    print(f"{'='*50}")
    print("\n  Clap twice to activate...\n")

    while True:
        try:
            detect_claps()
            print("\n  Activated!")
            speak("Yes sir?")
            command = listen_for_command()
            if not command:
                print("  No command heard. Clap again to retry.\n")
                speak("I didn't catch that. Try again.")
                continue

            print("  Asking local AI...")
            speak("On it.")
            try:
                actions = ask_ollama(command, workspaces)
            except Exception as e:
                print(f"  AI error: {e}")
                speak("Something went wrong. Please try again.")
                continue

            for action in actions:
                result = execute_action(action, workspaces)
                print(f"  Done: {result}")

            speak("Done.")
            print("\n  Clap twice to activate...\n")

        except KeyboardInterrupt:
            speak("Shutting down. Goodbye.")
            time.sleep(1.5)
            print("\n  JARVIS shutting down. Goodbye.")
            break

if __name__ == "__main__":
    main()