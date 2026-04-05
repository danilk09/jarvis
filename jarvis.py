#!/usr/bin/env python3
"""
JARVIS - Voice-Activated AI Assistant
Clap twice → speak your command → Claude figures out what to do
"""

import os
import sys
import json
import time
import subprocess
import webbrowser
import platform
import anthropic

try:
    import sounddevice as sd
    import numpy as np
    import speech_recognition as sr
except ImportError:
    print("Missing dependencies. Run: pip install sounddevice numpy SpeechRecognition anthropic")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
WORKSPACES_FILE = os.path.join(os.path.dirname(__file__), "workspaces.json")
CLAP_THRESHOLD  = 0.3      # 0.0 to 1.0 — raise if too sensitive, lower if not detecting
CLAP_GAP_MIN    = 0.15     # min seconds between two claps
CLAP_GAP_MAX    = 1.2      # max seconds between two claps
SAMPLE_RATE     = 44100
CHUNK           = 1024
OS              = platform.system()  # "Darwin" | "Windows" | "Linux"

# ── Workspaces ───────────────────────────────────────────────────────────────
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

# ── System Actions ────────────────────────────────────────────────────────────
def execute_action(action: dict, workspaces: dict) -> str:
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

# ── Claude Brain ──────────────────────────────────────────────────────────────
def ask_claude(command: str, workspaces: dict) -> list:
    workspace_list = json.dumps(list(workspaces.keys()), indent=2)
    client = anthropic.Anthropic()

    system = f"""You are JARVIS, a voice assistant. Parse the user's voice command and return a JSON array of actions to perform.

Available workspace names: {workspace_list}

Action types:
- {{"type": "workspace", "target": "<name>"}}
- {{"type": "url", "target": "<url>"}}
- {{"type": "search", "engine": "google|youtube|github", "query": "<q>"}}
- {{"type": "app", "target": "<app name>"}}
- {{"type": "vscode", "target": "<path>"}}
- {{"type": "file", "target": "<path>"}}
- {{"type": "terminal", "command": "<cmd>"}}

Match workspace names fuzzily. Return ONLY a JSON array, no explanation."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": command}]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

# ── Clap Detection (sounddevice) ──────────────────────────────────────────────
def detect_claps() -> bool:
    clap_times = []
    print("  👂 Listening for claps...")

    def callback(indata, frames, time_info, status):
        volume = float(np.abs(indata).max())
        if volume > CLAP_THRESHOLD:
            now = time.time()
            nonlocal clap_times
            clap_times = [t for t in clap_times if now - t < CLAP_GAP_MAX]
            if not clap_times or (now - clap_times[-1]) > CLAP_GAP_MIN:
                clap_times.append(now)
            if len(clap_times) >= 2:
                raise sd.CallbackStop()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        blocksize=CHUNK, callback=callback):
        while len(clap_times) < 2:
            time.sleep(0.05)

    return True

def listen_for_command(timeout=6) -> str:
    r = sr.Recognizer()
    r.energy_threshold = 300
    r.dynamic_energy_threshold = True
    with sr.Microphone() as source:
        print("  🎤 Listening for command...")
        r.adjust_for_ambient_noise(source, duration=0.4)
        try:
            audio = r.listen(source, timeout=timeout, phrase_time_limit=8)
        except sr.WaitTimeoutError:
            return ""
    try:
        text = r.recognize_google(audio)
        print(f'  Heard: "{text}"')
        return text
    except sr.UnknownValueError:
        print("  Could not understand audio.")
        return ""
    except sr.RequestError as e:
        print(f"  Speech recognition error: {e}")
        return ""

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable first.")
        print('  Windows: $env:ANTHROPIC_API_KEY="sk-ant-..."')
        sys.exit(1)

    workspaces = load_workspaces()
    print(f"\n{'='*50}")
    print("  JARVIS is ready")
    print(f"  Workspaces: {list(workspaces.keys()) or 'none'}")
    print(f"  Clap threshold: {CLAP_THRESHOLD}  (tune in jarvis.py if needed)")
    print(f"  OS: {OS}")
    print(f"{'='*50}")
    print("\n  👏 Clap twice to activate...\n")

    while True:
        try:
            detect_claps()
            print("\n  ✅ Activated!")
            command = listen_for_command()
            if not command:
                print("  No command heard. Clap again to retry.\n")
                continue

            print("  🤖 Asking Claude...")
            try:
                actions = ask_claude(command, workspaces)
            except Exception as e:
                print(f"  Claude error: {e}")
                continue

            for action in actions:
                result = execute_action(action, workspaces)
                print(f"  ✓ {result}")

            print("\n  👏 Clap twice to activate...\n")

        except KeyboardInterrupt:
            print("\n  JARVIS shutting down. Goodbye.")
            break

if __name__ == "__main__":
    main()