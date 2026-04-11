#!/usr/bin/env python3
"""
JARVIS - Voice-Activated AI Assistant
Clap twice -> speak your command -> local AI figures out what to do
"""

import concurrent.futures
import os
import sys
import json
import time
import subprocess
import webbrowser
import platform
import requests
import threading
import queue as _queue
import glob
import sqlite3
import shutil
import tempfile
import vosk
import queue
import json
import threading

try:
    import sounddevice as sd
    import numpy as np
    from faster_whisper import WhisperModel
    import soundfile as sf
except ImportError:
    print("Missing dependencies. Run: pip install sounddevice numpy faster-whisper soundfile")
    sys.exit(1)

print("  Loading Whisper model (first run may take a moment)...")
WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")

# ── Voice Engine ──────────────────────────────────────────────────────────────
# SPEECH_RATE: -10 (slow) to 10 (fast)
# VOICE_NAME: "Microsoft Guy" "Microsoft Davis" "Microsoft David" "Microsoft Zira"
# Install voices: Settings -> Time & Language -> Speech -> Add voices
VOICE_NAME  = ""
SPEECH_RATE = 1

_tts_queue = _queue.Queue()
_tts_ready = threading.Event()

def _tts_worker():
    _tts_ready.set()
    while True:
        text = _tts_queue.get()
        if text is None:
            break
        try:
            voice_line = f"$s.Voice = $s.GetVoices() | Where-Object {{$_.GetAttribute('Name') -like '*{VOICE_NAME}*'}} | Select-Object -First 1;" if VOICE_NAME else ""
            ps_cmd = (
                f"$s = New-Object -ComObject SAPI.SpVoice;"
                f"{voice_line}"
                f"$s.Rate = {SPEECH_RATE};"
                f"$s.Speak([System.String]::Concat('{text}')) | Out-Null"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                check=True, capture_output=True
            )
        except Exception as e:
            print(f"  TTS error: {e}")
        _tts_queue.task_done()

_tts_thread = threading.Thread(target=_tts_worker, daemon=True)
_tts_thread.start()
_tts_ready.wait()

def speak(text):
    print(f"  JARVIS: {text}")
    _tts_queue.put(text.replace("'", "''"))

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACES_FILE = os.path.join(os.path.dirname(__file__), "workspaces.json")
CLAP_THRESHOLD  = 0.6
CLAP_GAP_MIN    = 0.15
CLAP_GAP_MAX    = 1.2
SAMPLE_RATE     = 44100
CHUNK           = 1024
OS              = platform.system()

# ── File Index ────────────────────────────────────────────────────────────────
FILE_INDEX = []  # list of absolute file paths on the machine

def build_file_index():
    """Scan common user directories and build a file path index."""
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
                # Skip hidden and system folders
                dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in ['node_modules', '__pycache__', '.git']]
                for f in filenames:
                    paths.append(os.path.join(dirpath, f))
    FILE_INDEX = paths
    print(f"  File index built: {len(FILE_INDEX)} files found.")

def get_chrome_bookmarks():
    """Extract Chrome bookmark URLs and titles."""
    bookmarks = []
    chrome_path = os.path.expanduser(
        "~/AppData/Local/Google/Chrome/User Data/Default/Bookmarks"
    )
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
    """Extract Edge bookmark URLs and titles."""
    bookmarks = []
    edge_path = os.path.expanduser(
        "~/AppData/Local/Microsoft/Edge/User Data/Default/Bookmarks"
    )
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
HISTORY = []  # list of {"title": str, "url": str, "visited": datetime str}

def read_browser_history(db_path, limit=5000):
    """Read Chrome/Edge history SQLite DB. Must copy first — browser locks it."""
    entries = []
    if not os.path.exists(db_path):
        return entries
    tmp = os.path.join(tempfile.gettempdir(), "jarvis_history_tmp.db")
    try:
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        cur  = conn.cursor()
        # Chrome stores time as microseconds since 1601-01-01
        cur.execute("""
            SELECT title, url, last_visit_time
            FROM urls
            ORDER BY last_visit_time DESC
            LIMIT ?
        """, (limit,))
        epoch_offset = 11644473600  # seconds between 1601 and 1970
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
    # Deduplicate by URL, keep most recent visit
    seen = {}
    for e in all_entries:
        url = e["url"]
        if url not in seen or e["visited"] > seen[url]["visited"]:
            seen[url] = e
    HISTORY = sorted(seen.values(), key=lambda x: x["visited"], reverse=True)
    print(f"  History index built: {len(HISTORY)} unique pages found.")

def search_history(keyword=None, days_ago=None, limit=5):
    """Search history by keyword and/or recency."""
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
    """Try multiple strategies to open an app by name on Windows."""
    # Strategy 1: start command (works for many installed apps)
    try:
        subprocess.Popen(f'start "" "{name}"', shell=True)
        return True
    except Exception:
        pass
    # Strategy 2: try running the name directly as an exe
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
        # Exact substring match gets highest score
        if keyword in name:
            scored.append((100, p))
            continue
        # Word overlap score
        name_words = set(name_no_ext.replace("_", " ").replace("-", " ").split())
        overlap = len(kw_words & name_words)
        if overlap == 0:
            continue
        # Character similarity score
        shorter, longer = sorted([keyword, name_no_ext], key=len)
        char_score = sum(1 for c in shorter if c in longer) / max(len(longer), 1) * 100
        score = (overlap / max(len(kw_words), 1) * 60) + (char_score * 0.4)
        if score >= threshold:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]

def execute_action(action, workspaces):
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
        # Multiple matches — write results file and ask user
        results_path = os.path.join(os.path.dirname(__file__), "jarvis_matches.txt")
        with open(results_path, "w") as f:
            for i, p in enumerate(matches[:20], 1):
                f.write(f"{i}. {p}\n")
        os.startfile(results_path)
        speak(f"I found {min(len(matches), 20)} matches. I've opened a list on your desktop. Say the numbers you want opened.")
        # Listen for user response
        nums = listen_for_selection(max_attempts=3)
        if not nums:
            return "Selection cancelled."
        for n in nums:
            if 1 <= n <= len(matches):
                os.startfile(matches[n-1])
                time.sleep(0.3)
        return f"Opened {len(nums)} file(s): {', '.join(os.path.basename(matches[n-1]) for n in nums)}"

    elif kind == "find_files":
        keyword = action.get("keyword", target).lower()
        ext     = (action.get("extension") or "").lower()
        matches = fuzzy_match_files(keyword, FILE_INDEX, threshold=60, limit=20)
        if not matches:
            return f"No files found matching '{keyword}'"
        if len(matches) == 1:
            os.startfile(matches[0])
            return f"Opened {matches[0]}"
        results_path = os.path.join(os.path.dirname(__file__), "jarvis_matches.txt")
        with open(results_path, "w") as f:
            for i, p in enumerate(matches, 1):
                f.write(f"{i}. {p}\n")
        os.startfile(results_path)
        speak(f"Found {len(matches)} files. List is on your desktop. Say the numbers you want opened.")
        nums = listen_for_selection(max_attempts=3)
        if not nums:
            return "Selection cancelled."
        for n in nums:
            if 1 <= n <= len(matches):
                os.startfile(matches[n-1])
                time.sleep(0.3)
        return f"Opened {len(nums)} file(s): {', '.join(os.path.basename(matches[n-1]) for n in nums)}"

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

    elif kind == "none":
        return "No action"

    return f"Unknown action: {kind}"

# ── Ollama Brain ───────────────────────────────────────────────────────────────
CHAT_HISTORY    = []
IN_CONVERSATION = False

def build_system_prompt(workspaces):
    workspace_list = json.dumps(list(workspaces.keys()))
    bookmark_sample = json.dumps([b["name"] for b in BOOKMARKS[:30]])

    return f"""You are JARVIS. Return ONLY a single JSON object, no explanation.

WORKSPACES (use type "workspace"): {workspace_list}
BOOKMARKS searchable by keyword (use type "bookmark").
FILES searchable by keyword (use type "find_files").
HISTORY searchable by keyword (use type "history").

ACTION TYPES (pick one):
{{"mode":"action","actions":[{{"type":"workspace","target":"<name>"}}]}}
{{"mode":"action","actions":[{{"type":"url","target":"<full url>"}}]}}
{{"mode":"action","actions":[{{"type":"app","target":"<app name>"}}]}}
{{"mode":"action","actions":[{{"type":"search","engine":"google|youtube|github","query":"<q>"}}]}}
{{"mode":"action","actions":[{{"type":"vscode","target":"<path>"}}]}}
{{"mode":"action","actions":[{{"type":"find_files","keyword":"<word>","extension":"<or empty>"}}]}}
{{"mode":"action","actions":[{{"type":"bookmark","target":"<keyword>"}}]}}
{{"mode":"action","actions":[{{"type":"history","keyword":"<word>","days_ago":null}}]}}
{{"mode":"conversation","output":"speak","reply":"<response>"}}
{{"mode":"end_conversation"}}
{{"mode":"none"}}

RULES:
- "open [app]" → type "app"
- "open [bookmark keyword]" → type "bookmark"
- "find files" or "open file" → type "find_files" with keyword
- "open [workspace]" → type "workspace". Fuzzy match (Example: "311","three eleven","3-11" all match workspace "311")
- Words like "open","find","search","launch","show" → ALWAYS mode "action"
- "let's talk","chat","conversation" → mode "conversation"
- "back to commands","stop","exit" → mode "end_conversation"
- Unclear/filler → mode "none"
- in_conversation=true → stay in conversation mode unless user exits
"""

_ai_error_count = 0
_AI_ERROR_RESET_THRESHOLD = 3

def init_chat_history(workspaces):
    global CHAT_HISTORY
    system_msg = build_system_prompt(workspaces)
    CHAT_HISTORY = [{"role": "system", "content": system_msg}]

def init_ollama(workspaces):
    init_chat_history(workspaces)
    speak("Warming up. Give me a moment.")
    print("  Warming up AI model...")
    requests.post("http://localhost:11434/api/chat", json={
        "model": "llama3.2:3b",
        "messages": CHAT_HISTORY + [{"role": "user", "content": "open google"}],
        "stream": False
    }, timeout=60)
    print("  AI model ready!")
    speak("JARVIS online. Ready for your command.")

def ask_ollama(command, workspaces):
    """
    Sends a command to local Ollama and returns the JSON response.
    Automatically limits max_tokens depending on mode for speed.
    """
    global CHAT_HISTORY, IN_CONVERSATION

    history_snapshot = CHAT_HISTORY.copy()

    # Annotate message with conversation state
    annotated = f"[in_conversation={IN_CONVERSATION}] {command}"
    CHAT_HISTORY.append({"role": "user", "content": annotated})

    # Decide token limit
    max_tokens = 150 if not IN_CONVERSATION else 350  # command vs conversation

    try:
        response = requests.post("http://localhost:11434/api/chat", json={
            "model": "llama3.2:3b",  # faster local model
            "messages": CHAT_HISTORY,
            "max_tokens": max_tokens,
            "stream": False
        }, timeout=20)

        raw = response.json()["message"]["content"].strip()
        CHAT_HISTORY.append({"role": "assistant", "content": raw})

        # Keep history lean
        if len(CHAT_HISTORY) > 13:
            CHAT_HISTORY = CHAT_HISTORY[:1] + CHAT_HISTORY[-12:]

        # Strip markdown fences if model adds them
        # Strip markdown fences if model adds them
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.split("```")[0]

        # Try increasingly aggressive recovery strategies
        def try_parse(text):
            text = text.replace("\\ ", " ").replace("\\.", ".")  # fix escaped spaces/dots

            # Strategy 1: direct parse
            try:
                return json.loads(text)
            except Exception:
                pass
            # Strategy 2: find first { to matching closing }
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
            # Strategy 3: strip trailing garbage character by character
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
        global _ai_error_count

        if result:
            _ai_error_count = 0  # reset error count on success
            CHAT_HISTORY.append({"role": "assistant", "content": raw})  # only append if valid
            return result

        # Bad response — remove the user message we just added too so history stays clean
        
        _ai_error_count += 1
        CHAT_HISTORY = history_snapshot
        if _ai_error_count >= _AI_ERROR_RESET_THRESHOLD:
            print(f"  {_ai_error_count} consecutive AI errors — resetting chat history.")
            init_chat_history(workspaces)  # full reset
            _ai_error_count = 0
        print(f"  Could not parse AI response: {raw[:100]}")
        return {"mode": "none"}

    except requests.exceptions.Timeout:
        CHAT_HISTORY = history_snapshot
        print("  AI timed out.")
        return {"mode": "none"}
    except requests.exceptions.ConnectionError:
        CHAT_HISTORY = history_snapshot
        print("  Could not reach Ollama. Is it still running?")
        return {"mode": "none"}
    except Exception as e:
        CHAT_HISTORY = history_snapshot
        print(f"  Unexpected AI error: {e}")
        return {"mode": "none"}

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

def ask_ollama_async(command, workspaces):
    future = _executor.submit(ask_ollama, command, workspaces)
    return future

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

# ── Wake Word Detection ─────────────────────────────────────────────────────────

WAKE_WORD = "jarvis"
q = queue.Queue()

# Load Vosk model
model = vosk.Model("models/vosk-model-small-en-us-0.15")

def callback(indata, frames, time, status):
    q.put(bytes(indata))

def listen_for_wake_word(activated_event):
    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16',
                           channels=1, callback=callback):
        rec = vosk.KaldiRecognizer(model, 16000)
        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text = result.get("text", "")
                if WAKE_WORD in text.lower():
                    print("Wake word detected!")
                    activated_event.set()  # ← this was missing
                    return

# ── Voice Recording For File Selection ──────────────────────────────────────────

def listen_for_selection(max_attempts=3):
    """Keep listening until we hear numbers, up to max_attempts times."""
    for attempt in range(max_attempts):
        if attempt > 0:
            speak("I didn't catch that. Say the numbers you want, or say cancel.")
        selection = listen_for_command(max_duration=20, silence_duration=3.0)
        if not selection:
            continue
        if "cancel" in selection.lower():
            return []
        import re
        nums = [int(n) for n in re.findall(r'\b(\d+)\b', selection) if 1 <= int(n) <= 20]
        word_map = {"one":1,"two":2,"three":3,"four":4,"five":5,
                    "six":6,"seven":7,"eight":8,"nine":9,"ten":10}
        for word, num in word_map.items():
            if word in selection.lower():
                nums.append(num)
        nums = sorted(set(nums))
        if nums:
            return nums
    speak("No valid selection. Cancelling.")
    return []

# ── Voice Recording ────────────────────────────────────────────────────────────
def listen_for_command(max_duration=10, silence_threshold=0.04, silence_duration=2.0):
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
        if volume > silence_threshold * 2.5:
            speaking_started = True
            silent_chunks = 0
        elif speaking_started:
            silent_chunks += 1
        if chunk_count[0] >= max_chunks or (speaking_started and silent_chunks >= silence_chunks_needed):
            done[0] = True
            raise sd.CallbackStop()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        blocksize=CHUNK, dtype="float32", callback=callback):
        while not done[0]:
            time.sleep(0.01)

    if not frames or not speaking_started:
        print("  No speech detected.")
        return ""

    recording = np.concatenate(frames, axis=0)
    tmp_path = os.path.join(tempfile.gettempdir(), "_jarvis_tmp.wav")
    sf.write(tmp_path, recording, SAMPLE_RATE)

    print("  Processing speech...")
    try:
        segments, _ = WHISPER_MODEL.transcribe(
            tmp_path, 
            language="en", 
            vad_filter=True,           # voice activity detection — filters non-speech
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ))
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

# ── Handle AI Response ─────────────────────────────────────────────────────────
def handle_response(response, workspaces):
    global IN_CONVERSATION

    if response.get("mode") in ("find_files", "file", "bookmark", "history", "url", "app", "search", "workspace", "vscode"):
        response = {"mode": "action", "actions": [response]}

    mode = response.get("mode", "none")

    if mode == "action":
        actions = response.get("actions", [])
        actions = [a for a in actions if isinstance(a, dict)]
        if not actions:
            speak("I'm not sure what to open.")
            return
        for action in actions:
            result = execute_action(action, workspaces)
            print(f"  Done: {result}")
        if not all(a.get("type") == "none" for a in actions):
            speak("Done.")

    elif mode == "conversation":
        IN_CONVERSATION = True
        reply  = response.get("reply", "I'm not sure how to respond to that.")
        output = response.get("output", "speak")
        if output in ("text", "both"):
            print(f"\n  ── JARVIS ──────────────────────────\n  {reply}\n  ────────────────────────────────────\n")
        if output in ("speak", "both"):
            speak(reply)

    elif mode == "end_conversation":
        IN_CONVERSATION = False
        speak("Returning to command mode.")
        print("  Exited conversation mode.")

    elif mode == "none":
        print("  No action taken.")

    else:
        print(f"  Unknown mode: {mode}")

# ── Main ───────────────────────────────────────────────────────────────────────
LISTENING_FOR_ACTIVATION = True

def main():
    try:
        requests.get("http://localhost:11434", timeout=3)
    except Exception:
        print("ERROR: Ollama is not running.")
        print("  Open the Ollama app from your Start menu, then run this script again.")
        sys.exit(1)

    workspaces = load_workspaces()

    # Build indexes in background so startup isn't slow
    threading.Thread(target=build_file_index,    daemon=True).start()
    threading.Thread(target=build_bookmark_index, daemon=True).start()
    threading.Thread(target=build_history_index,  daemon=True).start()

    init_ollama(workspaces)

    print(f"\n{'='*50}")
    print("  JARVIS is ready")
    print(f"  Workspaces: {list(workspaces.keys()) or 'none'}")
    print(f"  AI: Ollama llama3.2 (local)")
    print(f"  OS: {OS}")
    print(f"{'='*50}")
    print("\n  Clap twice or say 'Jarvis' to activate...\n")
    print("  Tip: Say 'let's talk' to enter conversation mode.")
    print("  Tip: Say 'open Discord' / 'open Spotify' to launch apps.\n")

    # Event to signal activation (clap or wake word)
    activated_event = threading.Event()

    def clap_thread():
        global LISTENING_FOR_ACTIVATION
        while True:
            if LISTENING_FOR_ACTIVATION and detect_claps():
                print("Clap trigger detected!")
                activated_event.set()

    def voice_thread():
        global LISTENING_FOR_ACTIVATION
        while True:
            if LISTENING_FOR_ACTIVATION:
                listen_for_wake_word(activated_event)  # triggers activated_event inside

    threading.Thread(target=clap_thread, daemon=True).start()
    threading.Thread(target=voice_thread, daemon=True).start()

    # --- Main loop ---
    while True:
        try:
            # --- Command mode ---
            if not IN_CONVERSATION:
                print("Waiting for clap or wake-word...")
                LISTENING_FOR_ACTIVATION = True
                activated_event.wait()  # wait until triggered
                LISTENING_FOR_ACTIVATION = False
                activated_event.clear()
                print("\n  Activated!")
                speak("Yes sir.")
                _tts_queue.join()
            else:
                # Conversation mode
                print("  [Conversation mode] Speak anytime, or say 'back to commands'...")

            # --- Listen for user command ---
            command = listen_for_command()
            if not command:
                if IN_CONVERSATION:
                    continue  # keep listening in conversation
                print("  No command heard. Clap or say 'Jarvis' again.\n")
                speak("I didn't catch that. Try again.")
                continue

            print("  Thinking...")

            # --- Async AI call ---
            future = ask_ollama_async(command, workspaces)
            try:
                response = future.result(timeout=15)  # wait max 15s
            except Exception as e:
                print(f"  AI error: {e}")
                speak("Something went wrong. Please try again.")
                continue

            # --- Handle AI response ---
            handle_response(response, workspaces)

            if not IN_CONVERSATION:
                print("\n  Clap twice or say 'Jarvis' to activate...\n")

            LISTENING_FOR_ACTIVATION = True
            
        except KeyboardInterrupt:
            speak("Shutting down. Goodbye.")
            _tts_queue.join()
            print("\n  JARVIS shutting down. Goodbye.")
            break


if __name__ == "__main__":
    main()