#!/usr/bin/env python3
"""
Jarvis Pi Server — multi-session, multi-user.

Each browser tab gets its own session with isolated CHAT_HISTORY and state.
Users sign in with just a username; their workspaces are saved to users/<username>/workspaces.json.
Sleep mode: 10pm–6am. Users can override with the Wake button.

Device types: "desktop" (default), "phone" (future)
  Desktop: all actions including screenshot, vscode, coding_mode, music
  Phone:   workspace, url, search, web_search, generate_file, chat

Actions NOT supported (server cannot reach client desktop):
  "app"        — can't launch desktop apps remotely
  "find_files" — can't search user's local filesystem from server
  "bookmark"   — can't read user's Chrome/Edge bookmarks from server

Workarounds vs jarvis.py:
  "vscode"     — sends vscode://file/<path> protocol URL; client browser handles it
  "screenshot" — server sends request_screenshot; client captures + uploads via WS
  "music"      — yt-dlp resolves audio URL on Pi; browser plays via HTML5 Audio

Run:
    python jarvis_server.py

Dependencies (Pi):
    pip install fastapi uvicorn[standard] anthropic python-dotenv faster-whisper \
                requests yt-dlp Pillow pypdf
    sudo apt install ffmpeg
"""

import asyncio
import base64
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import requests as _requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BRAVE_API_KEY     = os.environ.get("BRAVE_API_KEY", "")
SERVER_HOST       = os.environ.get("JARVIS_HOST", "0.0.0.0")
SERVER_PORT       = int(os.environ.get("JARVIS_PORT", "5150"))
SLEEP_START       = 22   # 10 pm
SLEEP_END         = 6    # 6 am

BASE_DIR    = Path(__file__).parent
DB_PATH     = BASE_DIR / "jarvis_users.db"
USERS_DIR   = BASE_DIR / "users"
REACT_BUILD = BASE_DIR / "jarvis-dashboard" / "build"

_AUTOPLAY_SUFFIXES = ["", " mix", " radio", " similar artists", " playlist"]

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

print("  Loading Whisper model...")
WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
_whisper_lock = threading.Lock()
print("  Whisper ready.")

# ── SQLite user registry ───────────────────────────────────────────────────────
def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            username   TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tokens (
            token       TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
    """)
    # Migrate old schema that had a password_hash column
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
    if "password_hash" in cols:
        con.executescript("""
            CREATE TABLE users_new (username TEXT PRIMARY KEY, created_at TEXT NOT NULL);
            INSERT INTO users_new SELECT username, created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
        """)
    con.commit()
    con.close()

def _register(username: str) -> bool:
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT INTO users VALUES (?,?)",
            (username, datetime.now().isoformat()),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()

def _create_token(username: str) -> str:
    tok = secrets.token_hex(32)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO tokens VALUES (?,?,?)",
        (tok, username, datetime.now().isoformat()),
    )
    con.commit()
    con.close()
    return tok

def _revoke_token(token: str):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM tokens WHERE token=?", (token,))
    con.commit()
    con.close()

def _username_from_token(token: str) -> Optional[str]:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT username FROM tokens WHERE token=?", (token,)
    ).fetchone()
    con.close()
    return row[0] if row else None

# ── Per-user filesystem paths ──────────────────────────────────────────────────
def _user_dir(username: str) -> Path:
    d = USERS_DIR / username
    d.mkdir(parents=True, exist_ok=True)
    return d

def _workspaces_path(username: str) -> Path:
    return _user_dir(username) / "workspaces.json"

def _output_dir(username: str) -> Path:
    d = _user_dir(username) / "jarvis_output"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _input_dir(username: str) -> Path:
    d = _user_dir(username) / "jarvis_input"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _archive_dir(username: str) -> Path:
    d = _input_dir(username) / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _coding_dir(username: str) -> Path:
    d = _user_dir(username) / "coding"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _load_workspaces(username: str) -> dict:
    p = _workspaces_path(username)
    return json.loads(p.read_text()) if p.exists() else {}

def _save_workspaces(username: str, data: dict):
    _workspaces_path(username).write_text(json.dumps(data, indent=2))

# ── Auth helper ────────────────────────────────────────────────────────────────
def _auth_from_header(authorization: Optional[str] = None) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return _username_from_token(authorization[7:])

# ── Per-user state (polled by React dashboard) ─────────────────────────────────
_user_states: dict[str, dict] = {}
_states_lock  = threading.Lock()

_STATE_DEFAULTS = {
    "state": "idle", "transcript": "", "files": [],
    "music_playing": False, "music_paused": False,
    "current_song": "", "song_history": [],
}

def _get_state(username: str) -> dict:
    with _states_lock:
        return dict(_user_states.get(username, dict(_STATE_DEFAULTS)))

def _set_state(username: str, **kwargs):
    with _states_lock:
        s = _user_states.setdefault(username, dict(_STATE_DEFAULTS))
        s.update(kwargs)

def _push_file_to_state(username: str, name: str, content: str):
    entry = {"name": name, "content": content,
             "time": datetime.now().strftime("%H:%M:%S"),
             "size": f"{len(content):,} chars"}
    with _states_lock:
        s = _user_states.setdefault(username, dict(_STATE_DEFAULTS))
        s.setdefault("files", []).append(entry)

# ── Sleep mode ─────────────────────────────────────────────────────────────────
_sleep_mode      = False
_sleep_overrides = 0

def _is_sleep_time() -> bool:
    h = datetime.now().hour
    return h >= SLEEP_START or h < SLEEP_END

# ── Session dataclass ──────────────────────────────────────────────────────────
@dataclass
class Session:
    id:             str
    username:       str
    websocket:      WebSocket
    workspaces:     dict  = field(default_factory=dict)
    overrode_sleep: bool  = False
    chat_history:   list  = field(default_factory=list)
    error_count:    int   = 0
    device_type:    str   = "desktop"   # "desktop" | "phone" (future)
    # music state
    music_query:    str   = ""
    music_title:    str   = ""
    music_playing:  bool  = False
    music_paused:   bool  = False
    music_active:   bool  = False
    autoplay_count: int   = 0
    song_history:   list  = field(default_factory=list)
    session_archive: Optional[Path] = None

sessions:      dict[str, Session] = {}
_sessions_lock = asyncio.Lock()

# Apps that register OS-level URI protocol handlers the browser can trigger.
# Browsers can open these via window.open() even though they can't launch
# arbitrary executables.  Apps not in this map are not openable from the server.
_APP_PROTOCOLS: dict[str, str] = {
    "discord":        "discord://",
    "spotify":        "spotify://",
    "slack":          "slack://",
    "zoom":           "zoommtg://open",
    "steam":          "steam://",
    "figma":          "figma://",
    "notion":         "notion://",
    "obsidian":       "obsidian://",
    "vscode":         "vscode://",
    "visual studio code": "vscode://",
    "code":           "vscode://",
    "teams":          "msteams://",
    "microsoft teams":"msteams://",
    "skype":          "skype://",
    "telegram":       "tg://",
    "whatsapp":       "whatsapp://",
    "linear":         "linear://",
    "1password":      "onepassword://",
    "bitwarden":      "bitwarden://",
    "epic games":     "com.epicgames.launcher://",
    "epic":           "com.epicgames.launcher://",
    "twitch":         "twitch://",
    "xcode":          "xcode://",
}

# ── System prompt ──────────────────────────────────────────────────────────────
def _build_system_prompt(username: str, workspaces: dict, device_type: str = "desktop") -> str:
    ws_list = json.dumps(list(workspaces.keys()))
    desktop_actions = (
        '{"mode":"action","actions":[{"type":"app","target":"<app name>"}]}\n'
        '{"mode":"action","actions":[{"type":"vscode","target":"<path or empty>"}]}\n'
        '{"mode":"action","actions":[{"type":"screenshot","prompt":"<what to do with / analyze in the screenshot>"}]}\n'
        '{"mode":"action","actions":[{"type":"input_folder","prompt":"<what to do with the files in the input folder>"}]}\n'
        '{"mode":"action","actions":[{"type":"coding_mode","prompt":"<full description of project/feature to scaffold>"}]}\n'
        '{"mode":"action","actions":[{"type":"music","command":"play","query":"<song, artist, or genre>"}]}\n'
        '{"mode":"action","actions":[{"type":"music","command":"stop|pause|resume|skip|replay"}]}\n'
        '{"mode":"action","actions":[{"type":"music","command":"prev","n":<1-5>}]}\n'
    ) if device_type == "desktop" else ""

    desktop_rules = (
        '- "open [app name]" → type "app" with the app name; works for Discord, Spotify, Slack, Zoom, Steam, Teams, Figma, Notion, Obsidian, Twitch, etc.\n'
        '- "open VSCode" / "code [path]" → type "vscode"\n'
        '- "screenshot", "take a screenshot", "look at my screen", "what\'s on screen" → type "screenshot"; put intent in "prompt". Can chain with generate_file or coding_mode.\n'
        '- "process input folder", "check input", "analyze input", "look at input files", "enhance image" → type "input_folder"; put intent in "prompt". Can chain with generate_file.\n'
        '- "code [thing]", "build a project", "scaffold [thing]", "coding mode" → type "coding_mode"; full description in "prompt"\n'
        '- "play [song/artist/genre]" → type "music", command "play", query = what to play\n'
        '- "stop music", "turn off music" → type "music", command "stop"\n'
        '- "pause music", "pause" → type "music", command "pause"\n'
        '- "resume music", "unpause" → type "music", command "resume"\n'
        '- "skip", "next song" → type "music", command "skip"\n'
        '- "previous song", "go back", "last song" → type "music", command "prev", n=1\n'
        '- "previous [N]th song" → command "prev", n=N (1-5)\n'
        '- "replay", "play it again", "restart the song" → type "music", command "replay"\n'
    ) if device_type == "desktop" else ""

    return (
        f"You are JARVIS, a voice assistant for {username}. "
        "Return ONLY a single raw JSON object — no explanation, no markdown fences.\n"
        f"\nWORKSPACES (use type \"workspace\"): {ws_list}\n"
        "\nACTION TYPES (choose exactly one):\n"
        '{"mode":"action","actions":[{"type":"workspace","target":"<name>"}]}\n'
        '{"mode":"action","actions":[{"type":"url","target":"<full url>"}]}\n'
        '{"mode":"action","actions":[{"type":"search","engine":"google|youtube|github","query":"<q>"}]}\n'
        '{"mode":"action","actions":[{"type":"web_search","query":"<search query>"}]}\n'
        '{"mode":"action","actions":[{"type":"generate_file","filename":"<name.ext>","prompt":"<full description of what to write>","search_query":"<targeted web search query, or empty string>"}]}\n'
        + desktop_actions +
        '{"mode":"chat","reply":"<spoken answer>"}\n'
        '{"mode":"none"}\n\n'
        "RULES:\n"
        '- "open [workspace]" → type "workspace"; fuzzy-match name (e.g. "311","three eleven","3-11" all match "311")\n'
        '- "open [site]" / "go to [url]" → type "url"\n'
        '- "search [engine] for [q]" → type "search"\n'
        '- Real-time info needed (news, weather, sports, prices) → type "web_search"\n'
        '- "write/generate/create [file]" → type "generate_file"; always include a file extension; if content needs current data set "search_query", otherwise ""\n'
        + desktop_rules +
        '- Questions / conversation → mode "chat"; 1-2 spoken sentences, no markdown\n'
        '- Filler / unclear / cancel / never mind → mode "none"\n'
        '- Keep chat replies SHORT: 1-2 sentences. Plain spoken sentences only. No markdown, no lists.\n'
    )

def _init_history(username: str, workspaces: dict, device_type: str = "desktop") -> list[dict]:
    return [{"role": "system", "content": _build_system_prompt(username, workspaces, device_type)}]

# ── JSON parsing (robust, matches jarvis.py try_parse) ────────────────────────
def _repair_json_strings(raw: str) -> str:
    result, in_string, i = [], False, 0
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

def _parse_json(text: str) -> Optional[dict]:
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
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[start:i + 1])
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
    try:
        return json.loads(_repair_json_strings(text))
    except Exception:
        pass
    return None

# ── Brave search ───────────────────────────────────────────────────────────────
def _brave_search(query: str, count: int = 5) -> list[dict]:
    if not BRAVE_API_KEY:
        return []
    try:
        r = _requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
            params={"q": query, "count": count},
            timeout=8,
        )
        r.raise_for_status()
        return [
            {"title": x.get("title", ""), "description": x.get("description", ""), "url": x.get("url", "")}
            for x in r.json().get("web", {}).get("results", [])
        ]
    except Exception as e:
        print(f"  Brave error: {e}")
        return []

def _synthesize_answer(query: str, results: list[dict]) -> str:
    if not results:
        return "I couldn't find anything for that."
    snippets = "\n".join(
        f"{i+1}. {r['title']}: {r['description']} ({r['url']})"
        for i, r in enumerate(results)
    )
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="You are a voice assistant. Using the search results, give a concise spoken answer in 2-4 sentences. No markdown, no bullet points — plain conversational sentences only.",
            messages=[{"role": "user", "content": f"Question: {query}\n\nSearch results:\n{snippets}"}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Search failed: {e}"

# ── File generation ────────────────────────────────────────────────────────────
def _generate_file_content(prompt: str, filename: str, context: str = "") -> str:
    ext  = os.path.splitext(filename)[1].lower()
    lang = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".html": "HTML", ".css": "CSS", ".md": "Markdown",
        ".json": "JSON", ".sh": "Shell script", ".txt": "plain text",
    }.get(ext, "plain text")
    body = f"Real-time web data:\n{context}\n\nTask: {prompt}" if context else prompt
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            system=(
                f"You are a file generator. Output ONLY the raw file content — no explanation, "
                f"no preamble, no markdown fences. File: '{filename}' ({lang}). "
                f"Provide thorough, in-depth content using any real-time data supplied."
            ),
            messages=[{"role": "user", "content": body}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"# Generation failed: {e}"

def _summarize_file_for_speech(content: str, filename: str) -> str:
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            system="You are a voice assistant. In 1-2 spoken sentences, briefly summarize what was written. Be concise and conversational. No markdown.",
            messages=[{"role": "user", "content": f"Summarize what is in '{filename}':\n\n{content[:2500]}"}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return f"{filename} is ready."

# ── Coding skeleton ────────────────────────────────────────────────────────────
def _generate_coding_skeleton(prompt_text: str, context: str = "") -> Optional[dict]:
    user_content = f"Real-time web data:\n{context}\n\nTask: {prompt_text}" if context else prompt_text
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=(
                'You are a project scaffolding assistant. Output ONLY a raw JSON object:\n'
                '{"project_name":"<short_snake_case_name>","files":[{"path":"<relative_path>","content":"<file_content>"}],"summary":"<1 spoken sentence>"}\n'
                'JSON RULES: All string values must use \\n for newlines, \\t for tabs, \\\\ for backslashes, \\" for quotes. Never use literal newlines inside string values.\n'
                'Framework: For web/frontend/UI/dashboard projects scaffold as React+Vite. Include src/App.jsx, src/main.jsx, index.html, package.json, install.bat running "npm install".\n'
                'Always include: main entry point with skeleton+TODO comments, requirements.txt or package.json, install.bat, CLAUDE.md describing goal/structure/TODOs, .gitignore.\n'
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

# ── Image analysis ─────────────────────────────────────────────────────────────
def _sync_analyze_image(image_bytes: bytes, mime: str, prompt: str) -> str:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="You are a voice assistant. Answer only what was asked about the image in 1-3 concise sentences. No markdown — plain spoken sentences only.",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt or "Describe this image concisely."}
            ]}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Image analysis failed: {e}"

async def _analyze_image(image_bytes: bytes, mime: str, prompt: str, session: "Session") -> str:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _sync_analyze_image, image_bytes, mime, prompt)
    session.chat_history.append({"role": "user",      "content": f"[Image analysis request] {prompt}"})
    session.chat_history.append({"role": "assistant",  "content": f"[Image analysis result] {result}"})
    return result

# ── Pillow image enhancement ───────────────────────────────────────────────────
def _sync_pillow_enhance(image_bytes: bytes, mime: str, prompt: str, out_path: Path) -> str:
    try:
        from PIL import Image, ImageEnhance
    except ImportError:
        return "Pillow not installed — run: pip install Pillow"
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": (
                    f"User wants: '{prompt}'. Return ONLY JSON: "
                    '{"brightness":1.0,"contrast":1.0,"sharpness":1.0,"color":1.0,"description":"..."} '
                    "1.0=no change, range 0.5-2.0 (sharpness up to 3.0)."
                )}
            ]}]
        )
        raw = resp.content[0].text.strip()
        params = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except Exception as e:
        return f"Enhancement params failed: {e}"
    try:
        import io as _io
        img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
        img = ImageEnhance.Brightness(img).enhance(params.get("brightness", 1.0))
        img = ImageEnhance.Contrast(img).enhance(params.get("contrast", 1.0))
        img = ImageEnhance.Sharpness(img).enhance(params.get("sharpness", 1.0))
        img = ImageEnhance.Color(img).enhance(params.get("color", 1.0))
        img.save(str(out_path))
        return params.get("description", "Enhancement applied.")
    except Exception as e:
        return f"Pillow enhancement failed: {e}"

# ── PDF reading ────────────────────────────────────────────────────────────────
def _read_pdf(path: Path) -> str:
    try:
        import importlib
        pypdf = importlib.import_module("pypdf")
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            return "\n".join(p.extract_text() or "" for p in reader.pages[:10])[:4000]
    except Exception:
        pass
    try:
        import PyPDF2
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            return "\n".join(p.extract_text() or "" for p in reader.pages[:10])[:4000]
    except Exception as e:
        return f"(could not read PDF: {e})"

# ── Input folder processing ────────────────────────────────────────────────────
def _archive_input_file(src: Path, archive: Path):
    archive.mkdir(parents=True, exist_ok=True)
    dest = archive / src.name
    if dest.exists():
        dest = archive / f"{src.stem}_{int(datetime.now().timestamp())}{src.suffix}"
    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        print(f"  Could not archive {src.name}: {e}")

async def _process_input_folder(username: str, prompt: str, session: "Session") -> dict:
    input_d   = _input_dir(username)
    archive_d = session.session_archive or _archive_dir(username)

    all_items = [input_d / fn for fn in os.listdir(input_d) if (input_d / fn).is_file()]
    if not all_items:
        return {"speech": "No files found in the input folder.", "context": ""}

    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    TEXT_EXTS  = {".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx",
                  ".html", ".css", ".json", ".csv", ".xml", ".yaml", ".yml",
                  ".sh", ".bat", ".java", ".c", ".cpp", ".h", ".rs", ".go",
                  ".rb", ".php", ".sql", ".toml", ".ini", ".cfg"}

    enhance_kw = ["enhance", "improve", "fix", "sharpen", "brighten",
                  "denoise", "clean up", "make better", "increase contrast", "upscale"]
    wants_enhancement = any(kw in (prompt or "").lower() for kw in enhance_kw)

    context_parts, archived = [], []
    loop = asyncio.get_event_loop()

    for fpath in all_items:
        ext, fname = fpath.suffix.lower(), fpath.name

        if ext in IMAGE_EXTS:
            image_bytes = fpath.read_bytes()
            media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
            mime = media_map.get(ext, "image/png")
            if wants_enhancement:
                ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_path = _output_dir(username) / f"enhanced_{ts}.png"
                result   = await loop.run_in_executor(
                    None, _sync_pillow_enhance, image_bytes, mime, prompt, out_path
                )
                if out_path.exists():
                    _push_file_to_state(username, out_path.name, f"[enhanced image: {result}]")
                context_parts.append(f"[Image: {fname}] Enhanced: {result}")
            else:
                result = await _analyze_image(image_bytes, mime, prompt or "Describe this image.", session)
                context_parts.append(f"[Image: {fname}] {result}")
            archived.append(fpath)

        elif ext == ".txt":
            try:
                raw  = fpath.read_text(encoding="utf-8", errors="ignore").strip()
                urls = re.findall(r"https?://\S+", raw)
                if urls and len(raw.split()) <= 15:
                    for url in urls[:3]:
                        try:
                            r = await loop.run_in_executor(
                                None, lambda u=url: _requests.get(
                                    u, timeout=10, headers={"User-Agent": "Mozilla/5.0"}
                                )
                            )
                            clean = re.sub(r"<[^>]+>", " ", r.text)
                            clean = re.sub(r"\s+", " ", clean).strip()[:4000]
                            context_parts.append(f"[URL: {url}]\n{clean}")
                        except Exception as e:
                            context_parts.append(f"[URL: {url}] (fetch failed: {e})")
                else:
                    context_parts.append(f"[File: {fname}]\n{raw[:4000]}")
                archived.append(fpath)
            except Exception as e:
                context_parts.append(f"[File: {fname}] (read error: {e})")

        elif ext in TEXT_EXTS:
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
                context_parts.append(f"[File: {fname}]\n{content[:4000]}")
                archived.append(fpath)
            except Exception as e:
                context_parts.append(f"[File: {fname}] (read error: {e})")

        elif ext == ".pdf":
            pdf_text = await loop.run_in_executor(None, _read_pdf, fpath)
            context_parts.append(f"[PDF: {fname}]\n{pdf_text}")
            archived.append(fpath)

        else:
            context_parts.append(f"[File: {fname}] (unsupported type — skipped)")

    for fpath in archived:
        _archive_input_file(fpath, archive_d)

    all_context = "\n\n".join(context_parts)
    if not all_context.strip():
        return {"speech": "Could not read any content from the input folder.", "context": ""}

    session.chat_history.append({"role": "user", "content": f"[Input folder contents]\n{all_context}"})

    summary_prompt = prompt or "Briefly describe what's in these files in 2-3 spoken sentences."
    try:
        speech = await loop.run_in_executor(
            None, lambda: ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                system="You are a voice assistant. Answer in 1-3 concise spoken sentences. No markdown, no lists.",
                messages=[{"role": "user", "content": f"Context:\n{all_context[:5000]}\n\nRequest: {summary_prompt}"}],
            ).content[0].text.strip()
        )
        session.chat_history.append({"role": "assistant", "content": speech})
    except Exception:
        speech = f"Processed {len(archived)} item(s) from the input folder."

    return {"speech": speech, "context": all_context}

# ── Music URL resolution ───────────────────────────────────────────────────────
def _sync_resolve_music_url(query: str) -> tuple[str, str]:
    """Returns (audio_url, title) via yt-dlp, or ("", "") on failure."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--no-playlist", "-f", "bestaudio",
             "--print", "%(title)s", "--print", "%(urls)s", f"ytsearch1:{query}"],
            capture_output=True, text=True, timeout=30
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return "", ""
        url   = lines[-1]
        title = "\n".join(lines[:-1])
        if not url or not url.startswith("http"):
            return "", ""
        return url, title
    except FileNotFoundError:
        print("  yt-dlp not installed — run: pip install yt-dlp")
        return "", ""
    except Exception as e:
        print(f"  yt-dlp error: {e}")
        return "", ""

async def _resolve_music_url(query: str) -> tuple[str, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_resolve_music_url, query)

# ── Audio transcription ────────────────────────────────────────────────────────
async def _transcribe(audio_bytes: bytes, mime: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_transcribe, audio_bytes, mime)

def _sync_transcribe(audio_bytes: bytes, mime: str) -> str:
    ext = ".wav" if "wav" in mime else ".mp4" if ("mp4" in mime or "m4a" in mime) else ".webm"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(audio_bytes)
        src = f.name
    wav = src.rsplit(".", 1)[0] + "_16k.wav"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", "-f", "wav", wav],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            print(f"  ffmpeg error: {r.stderr.decode()[:200]}")
            return ""
        with _whisper_lock:
            segs, _ = WHISPER_MODEL.transcribe(
                wav, language="en", vad_filter=True,
                initial_prompt=(
                    "open workspace, search for, play music, pause, resume, stop music, "
                    "take a screenshot, process input folder, code a project, "
                    "never mind, stop, yes, no, cancel"
                ),
                vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
            )
        return " ".join(s.text.strip() for s in segs).strip()
    except Exception as e:
        print(f"  Transcribe error: {e}")
        return ""
    finally:
        for p in [src, wav]:
            try:
                os.remove(p)
            except Exception:
                pass

# ── Claude ─────────────────────────────────────────────────────────────────────
_BYPASS = {
    "never mind", "nevermind", "forget it", "forget that", "cancel", "stop",
    "nothing", "nope", "no", "abort", "disregard", "ignore that",
    "never mind that", "scratch that", "skip it",
    "um", "uh", "hmm", "hm", "okay", "ok", "yeah", "yes", "alright",
    "thanks", "thank you", "cool", "got it",
}

async def _ask_claude(session: "Session", command: str) -> dict:
    if command.strip().lower().rstrip(".,!?") in _BYPASS:
        return {"mode": "none"}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_ask_claude, session, command)

def _sync_ask_claude(session: "Session", command: str) -> dict:
    snapshot = session.chat_history.copy()
    session.chat_history.append({"role": "user", "content": command})
    messages = [m for m in session.chat_history if m["role"] != "system"]
    system   = next((m["content"] for m in session.chat_history if m["role"] == "system"), "")

    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=messages,
        )
        raw = resp.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.split("```")[0]

        result = _parse_json(raw)
        if result:
            # Normalize bare action dicts (model sometimes omits wrapper)
            if result.get("mode") in (
                "workspace", "url", "search", "vscode", "screenshot",
                "input_folder", "web_search", "generate_file",
                "coding_mode", "music",
            ):
                result = {"mode": "action", "actions": [result]}

            session.error_count = 0
            session.chat_history.append({"role": "assistant", "content": raw})
            if len(session.chat_history) > 13:
                session.chat_history = session.chat_history[:1] + session.chat_history[-12:]
            return result

        session.error_count += 1
        session.chat_history = snapshot
        if session.error_count >= 3:
            session.chat_history = _init_history(
                session.username, session.workspaces, session.device_type
            )
            session.error_count = 0
        return {"mode": "none"}

    except Exception as e:
        session.chat_history = snapshot
        print(f"  Claude error [{session.username}]: {e}")
        return {"mode": "none"}

# ── Action execution ───────────────────────────────────────────────────────────
async def _execute_action(
    action:     dict,
    workspaces: dict,
    username:   str,
    session:    "Session",
    websocket:  WebSocket,
    chain_ctx:  dict,
) -> tuple[str, list[dict]]:
    """Returns (spoken_reply, client_actions_list)."""
    kind   = action.get("type", "")
    target = action.get("target", "")
    query  = action.get("query", "")

    # ── Workspace ──────────────────────────────────────────────────────────────
    if kind == "workspace":
        ws = workspaces.get(target)
        if not ws:
            for k, v in workspaces.items():
                if target.lower() in k.lower() or k.lower() in target.lower():
                    ws, target = v, k
                    break
        if not ws:
            return f"No workspace named '{target}'.", []
        client_actions = []
        for item in ws.get("items", []):
            t, p = item.get("type"), item.get("path", "")
            if t == "url":
                client_actions.append({"type": "open_url", "url": p})
            elif t == "vscode":
                encoded = p.replace("\\", "/")
                client_actions.append({"type": "open_url", "url": f"vscode://file/{encoded}"})
        return f"Opening workspace {target}.", client_actions

    # ── URL ────────────────────────────────────────────────────────────────────
    elif kind == "url":
        return f"Opening {target}.", [{"type": "open_url", "url": target}]

    # ── VSCode ─────────────────────────────────────────────────────────────────
    elif kind == "vscode":
        if target:
            encoded = target.replace("\\", "/")
            return f"Opening {target} in VSCode.", [{"type": "open_url", "url": f"vscode://file/{encoded}"}]
        return "Opening VSCode.", [{"type": "open_url", "url": "vscode://"}]

    # ── App (via OS protocol handler) ──────────────────────────────────────────
    elif kind == "app":
        name = target.lower().strip()
        protocol = _APP_PROTOCOLS.get(name)
        if not protocol:
            # Fuzzy: try any key that the spoken name contains or vice-versa
            for key, proto in _APP_PROTOCOLS.items():
                if key in name or name in key:
                    protocol = proto
                    break
        if protocol:
            return f"Opening {target}.", [{"type": "open_url", "url": protocol}]
        return (
            f"I can't open {target} from the server. It doesn't have a registered "
            "protocol handler. You can add it as a workspace URL item instead.",
            []
        )

    # ── Search ─────────────────────────────────────────────────────────────────
    elif kind == "search":
        engine = action.get("engine", "google")
        urls = {
            "google":  f"https://google.com/search?q={query.replace(' ', '+')}",
            "youtube": f"https://youtube.com/results?search_query={query.replace(' ', '+')}",
            "github":  f"https://github.com/search?q={query.replace(' ', '+')}",
        }
        return f"Searching {engine}.", [{"type": "open_url", "url": urls.get(engine, urls["google"])}]

    # ── Web search ─────────────────────────────────────────────────────────────
    elif kind == "web_search":
        if not BRAVE_API_KEY:
            return "Web search isn't configured. Add BRAVE_API_KEY to your .env file.", []
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _brave_search, query or target)
        answer  = await loop.run_in_executor(None, _synthesize_answer, query or target, results)
        session.chat_history.append({"role": "user",      "content": f"[Web search] {query or target}"})
        session.chat_history.append({"role": "assistant",  "content": answer})
        return answer, []

    # ── Generate file ──────────────────────────────────────────────────────────
    elif kind == "generate_file":
        filename     = action.get("filename", "output.txt")
        prompt_text  = action.get("prompt", "")
        search_query = action.get("search_query", "")
        loop         = asyncio.get_event_loop()
        context      = chain_ctx.get("file_context", "") or chain_ctx.get("image_context", "")
        if search_query and BRAVE_API_KEY:
            results = await loop.run_in_executor(None, _brave_search, search_query, 6)
            if results:
                web_ctx = "\n".join(f"{r['title']}: {r['description']} ({r['url']})" for r in results)
                context = web_ctx + ("\n\n" + context if context else "")
        content = await loop.run_in_executor(None, _generate_file_content, prompt_text, filename, context)
        safe_name = re.sub(r"[^\w\-. ]", "_", filename)
        out_path  = _output_dir(username) / safe_name
        out_path.write_text(content, encoding="utf-8")
        _push_file_to_state(username, filename, content)
        summary = await loop.run_in_executor(None, _summarize_file_for_speech, content, filename)
        return summary, [{"type": "download_file", "filename": filename, "content": content}]

    # ── Input folder ───────────────────────────────────────────────────────────
    elif kind == "input_folder":
        prompt = action.get("prompt", "")
        output = await _process_input_folder(username, prompt, session)
        chain_ctx["file_context"] = output["context"]
        return output["speech"], []

    # ── Screenshot ─────────────────────────────────────────────────────────────
    elif kind == "screenshot":
        prompt = action.get("prompt", "")
        await websocket.send_json({"type": "request_screenshot"})
        try:
            ss_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            if ss_msg.get("type") == "screenshot_data":
                img_b64 = ss_msg.get("data", "")
                mime    = ss_msg.get("mime", "image/png")
                loop    = asyncio.get_event_loop()
                result  = await _analyze_image(
                    base64.b64decode(img_b64),
                    mime,
                    prompt or "Describe what's on screen.",
                    session
                )
                chain_ctx["image_context"] = result
                return result, []
            return "Screenshot cancelled or not supported by this client.", []
        except asyncio.TimeoutError:
            return "Screenshot timed out. The client may not support screen capture.", []

    # ── Coding mode ────────────────────────────────────────────────────────────
    elif kind == "coding_mode":
        prompt_text = action.get("prompt", "")
        loop        = asyncio.get_event_loop()
        context     = chain_ctx.get("file_context", "") or chain_ctx.get("image_context", "")
        if BRAVE_API_KEY:
            results = await loop.run_in_executor(None, _brave_search, prompt_text[:200], 4)
            if results:
                web_ctx = "\n".join(f"{r['title']}: {r['description']}" for r in results)
                context = web_ctx + ("\n\n" + context if context else "")
        skeleton = await loop.run_in_executor(None, _generate_coding_skeleton, prompt_text, context)
        if not skeleton:
            return "Project skeleton generation failed. Try again.", []
        project_name = re.sub(r"[^\w-]", "_", skeleton.get("project_name", "project")).strip("_") or "project"
        project_dir  = _coding_dir(username) / project_name
        project_dir.mkdir(parents=True, exist_ok=True)

        # Write files + push to dashboard + collect for zip
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in skeleton.get("files", []):
                rel = f.get("path", "").lstrip("/\\")
                if not rel:
                    continue
                full_path = project_dir / rel
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(f.get("content", ""), encoding="utf-8")
                _push_file_to_state(username, rel, f.get("content", ""))
                zf.writestr(rel, f.get("content", ""))
        zip_buf.seek(0)
        zip_b64 = base64.b64encode(zip_buf.read()).decode("utf-8")

        summary = skeleton.get("summary", f"Project {project_name} is ready.")
        return summary, [
            {"type": "download_file", "filename": f"{project_name}.zip",
             "content": zip_b64, "encoding": "base64"}
        ]

    # ── Music ──────────────────────────────────────────────────────────────────
    elif kind == "music":
        command = action.get("command", "play")

        if command == "play":
            q = action.get("query", "")
            if not q:
                return "What would you like to play?", []
            url, title = await _resolve_music_url(q)
            if not url:
                return "Couldn't find that song. yt-dlp may not be installed.", []
            if session.music_query:
                session.song_history.append(session.music_query)
                if len(session.song_history) > 5:
                    session.song_history.pop(0)
            session.music_query    = q
            session.music_title    = title
            session.music_playing  = True
            session.music_paused   = False
            session.music_active   = True
            session.autoplay_count = 0
            _set_state(session.username,
                       music_playing=True, music_paused=False,
                       current_song=title, song_history=list(session.song_history))
            return f"Playing {q}.", [{"type": "play_audio", "url": url, "title": title}]

        elif command == "stop":
            session.music_active  = False
            session.music_playing = False
            session.music_paused  = False
            _set_state(session.username, music_playing=False, music_paused=False, current_song="")
            return "Stopping music.", [{"type": "stop_audio"}]

        elif command == "pause":
            session.music_paused = True
            _set_state(session.username, music_paused=True)
            return "Pausing music.", [{"type": "pause_audio"}]

        elif command == "resume":
            session.music_paused = False
            _set_state(session.username, music_paused=False)
            return "Resuming music.", [{"type": "resume_audio"}]

        elif command == "skip":
            if not session.music_query:
                return "No music is playing.", []
            session.autoplay_count += 1
            suffix = _AUTOPLAY_SUFFIXES[session.autoplay_count % len(_AUTOPLAY_SUFFIXES)]
            url, title = await _resolve_music_url(session.music_query + suffix)
            if not url:
                return "Couldn't find a track to skip to.", []
            session.music_title   = title
            session.music_paused  = False
            _set_state(session.username, current_song=title, music_paused=False)
            return "Skipping to next track.", [{"type": "play_audio", "url": url, "title": title}]

        elif command == "prev":
            n = int(action.get("n", 1))
            if not session.song_history:
                return "No previous song in history.", []
            idx = len(session.song_history) - n
            if idx < 0:
                return f"Only {len(session.song_history)} song(s) in history.", []
            prev_query = session.song_history[idx]
            url, title = await _resolve_music_url(prev_query)
            if not url:
                return "Couldn't find the previous song.", []
            session.music_title    = title
            session.music_paused   = False
            session.autoplay_count = 0
            session.music_query    = prev_query
            _set_state(session.username, current_song=title, music_paused=False)
            return f"Playing {prev_query}.", [{"type": "play_audio", "url": url, "title": title}]

        elif command == "replay":
            if not session.music_query:
                return "No song is currently playing.", []
            url, title = await _resolve_music_url(session.music_query)
            if not url:
                return "Couldn't reload the current song.", []
            session.music_title    = title
            session.music_paused   = False
            session.autoplay_count = 0
            _set_state(session.username, current_song=title, music_paused=False)
            return "Replaying current song.", [{"type": "play_audio", "url": url, "title": title}]

        return "Unknown music command.", []

    return "", []

# ── Sleep mode broadcast ───────────────────────────────────────────────────────
async def _broadcast_sleep():
    async with _sessions_lock:
        for s in list(sessions.values()):
            try:
                await s.websocket.send_json({
                    "type": "sleeping",
                    "message": f"Jarvis is resting until {SLEEP_END}am. Tap Wake to override.",
                })
            except Exception:
                pass

async def _sleep_scheduler():
    global _sleep_mode
    while True:
        await asyncio.sleep(30)
        in_window = _is_sleep_time()
        if in_window and not _sleep_mode and _sleep_overrides == 0:
            _sleep_mode = True
            print("  Sleep mode activated.")
            await _broadcast_sleep()
        elif not in_window and _sleep_mode:
            _sleep_mode = False
            print("  Sleep mode deactivated.")

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def _on_startup():
    global _sleep_mode
    _init_db()
    USERS_DIR.mkdir(exist_ok=True)
    asyncio.create_task(_sleep_scheduler())
    if _is_sleep_time():
        _sleep_mode = True
        print("  Started in sleep mode.")

# ── Auth endpoints ─────────────────────────────────────────────────────────────
@app.post("/api/login")
async def api_login(request: Request):
    body     = await request.json()
    username = body.get("username", "").strip().lower()
    if not username:
        return JSONResponse({"error": "Username required."}, 400)
    if len(username) < 2 or len(username) > 32:
        return JSONResponse({"error": "Username must be 2–32 characters."}, 400)
    if not re.match(r"^[a-z0-9_-]+$", username):
        return JSONResponse({"error": "Username may only contain letters, numbers, - and _."}, 400)
    _register(username)  # no-op if user already exists
    _user_dir(username)
    token = _create_token(username)
    return {"token": token, "username": username}

@app.post("/api/logout")
async def api_logout(request: Request, authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    if authorization and authorization.startswith("Bearer "):
        _revoke_token(authorization[7:])
    return {"ok": True}

# ── Status (per-user, polled by React dashboard) ──────────────────────────────
@app.get("/status")
async def api_status(authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    return _get_state(username)

# ── Workspaces (per-user) ─────────────────────────────────────────────────────
@app.get("/api/workspaces")
async def api_get_workspaces(authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    return _load_workspaces(username)

@app.post("/api/workspaces")
async def api_save_workspaces(request: Request, authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    data = await request.json()
    _save_workspaces(username, data)
    async with _sessions_lock:
        for s in sessions.values():
            if s.username == username:
                s.workspaces   = data
                s.chat_history = _init_history(username, data, s.device_type)
    return {"ok": True}

# ── Music control (per-user, used by dashboard buttons) ───────────────────────
@app.post("/api/music/control")
async def api_music_control(request: Request, authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    data = await request.json()
    cmd  = data.get("command", "")
    async with _sessions_lock:
        user_sessions = [s for s in sessions.values() if s.username == username]
    if not user_sessions:
        return JSONResponse({"error": "No active session."}, 404)
    for s in user_sessions:
        try:
            await s.websocket.send_json({"type": "music_control", "command": cmd, "n": data.get("n", 1)})
        except Exception:
            pass
    return {"ok": True}

# ── Input folder (per-user) ───────────────────────────────────────────────────
def _guess_file_type(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):  return 'image'
    if ext == '.pdf':                                        return 'pdf'
    if ext in ('.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.c', '.cpp',
               '.h', '.rs', '.go', '.rb', '.php', '.sql'):  return 'code'
    if ext in ('.txt', '.md', '.csv', '.json', '.xml', '.yaml', '.yml',
               '.toml', '.ini', '.cfg', '.sh', '.bat', '.html', '.css'):
        return 'text'
    return 'file'

@app.get("/api/input")
async def api_input_list(authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    folder = _input_dir(username)
    files  = []
    for fname in os.listdir(folder):
        fpath = folder / fname
        if fpath.is_file():
            stat = fpath.stat()
            files.append({
                "name":     fname,
                "size":     stat.st_size,
                "modified": stat.st_mtime,
                "type":     _guess_file_type(fname),
            })
    files.sort(key=lambda f: f["modified"], reverse=True)
    return {"files": files}

@app.post("/api/input/upload")
async def api_input_upload(request: Request, authorization: Optional[str] = Header(None)):
    from fastapi import UploadFile
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    form   = await request.form()
    upload: UploadFile = form.get("file")
    if not upload or not upload.filename:
        return JSONResponse({"error": "No file provided."}, 400)
    safe_name = re.sub(r"[^\w\-. ]", "_", os.path.basename(upload.filename))
    dest = _input_dir(username) / safe_name
    dest.write_bytes(await upload.read())
    return {"ok": True, "filename": safe_name}

@app.get("/api/input/archive")
async def api_input_archive(authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    archive     = _archive_dir(username)
    sessions_out = []
    for sess in sorted(os.listdir(archive), reverse=True):
        sess_path = archive / sess
        if sess_path.is_dir():
            files = [
                {"name": fn, "size": (sess_path / fn).stat().st_size, "type": _guess_file_type(fn)}
                for fn in os.listdir(sess_path) if (sess_path / fn).is_file()
            ]
            sessions_out.append({"session": sess, "files": files})
    return {"sessions": sessions_out}

@app.post("/api/input/restore")
async def api_input_restore(request: Request, authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    body     = await request.json()
    session  = body.get("session", "")
    filename = body.get("filename", "")
    if not session or not filename:
        return JSONResponse({"error": "Missing session or filename."}, 400)
    src = _archive_dir(username) / session / filename
    if not src.exists():
        return JSONResponse({"error": "File not found."}, 404)
    dest = _input_dir(username) / filename
    if dest.exists():
        base, ext = os.path.splitext(filename)
        dest = _input_dir(username) / f"{base}_{int(datetime.now().timestamp())}{ext}"
    src.rename(dest)
    return {"ok": True}

# ── Output files (per-user) ───────────────────────────────────────────────────
@app.get("/api/output/{filename}")
async def api_output_download(filename: str, authorization: Optional[str] = Header(None)):
    username = _auth_from_header(authorization)
    if not username:
        return JSONResponse({"error": "Unauthorized."}, 401)
    safe = re.sub(r"[^\w\-. ]", "_", filename)
    path = _output_dir(username) / safe
    if not path.exists():
        return JSONResponse({"error": "File not found."}, 404)
    return FileResponse(path, filename=safe)

# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    global _sleep_mode, _sleep_overrides
    await websocket.accept()
    session: Optional[Session] = None

    try:
        # ── Auth phase ──────────────────────────────────────────────────────
        try:
            auth = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        except asyncio.TimeoutError:
            await websocket.close(code=4001, reason="auth timeout")
            return

        if auth.get("type") != "auth":
            await websocket.close(code=4002, reason="expected auth message first")
            return

        token    = auth.get("token", "")
        username = _username_from_token(token) if token else None

        if not username:
            await websocket.send_json({"type": "error", "message": "Invalid or missing token. Please log in again."})
            await websocket.close(code=4003)
            return

        device_type = auth.get("device_type", "desktop")

        # ── Sleep gate ───────────────────────────────────────────────────────
        overrode = False
        if _sleep_mode:
            await websocket.send_json({
                "type": "sleeping",
                "message": f"Jarvis is resting until {SLEEP_END}am. Tap Wake to override.",
            })
            try:
                wake_msg = await asyncio.wait_for(websocket.receive_json(), timeout=60)
            except asyncio.TimeoutError:
                await websocket.close()
                return
            if wake_msg.get("type") != "wake":
                await websocket.close()
                return
            _sleep_mode = False
            _sleep_overrides += 1
            overrode = True
            print(f"  Sleep overridden by {username}.")

        # ── Create session ───────────────────────────────────────────────────
        workspaces = _load_workspaces(username)
        ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
        session    = Session(
            id=str(uuid.uuid4()),
            username=username,
            websocket=websocket,
            workspaces=workspaces,
            overrode_sleep=overrode,
            chat_history=_init_history(username, workspaces, device_type),
            device_type=device_type,
            session_archive=_archive_dir(username) / f"session_{ts}",
        )
        async with _sessions_lock:
            sessions[session.id] = session

        _set_state(username, state="idle")
        print(f"  [{username}] connected ({session.id[:8]}, {device_type}) — {len(sessions)} active")

        await websocket.send_json({"type": "auth_ok", "username": username})

        # ── Command loop ─────────────────────────────────────────────────────
        while True:
            try:
                msg = await websocket.receive_json()
            except Exception:
                break

            msg_type = msg.get("type")

            if msg_type == "disconnect":
                break

            # ── Wake override ────────────────────────────────────────────────
            elif msg_type == "wake":
                if _is_sleep_time() and not session.overrode_sleep:
                    _sleep_mode = False
                    _sleep_overrides += 1
                    session.overrode_sleep = True
                    print(f"  Mid-session wake by {username}.")
                _set_state(username, state="idle")
                await websocket.send_json({"type": "awake", "message": "I'm awake. What do you need?"})

            # ── Audio ended (autoplay next track) ────────────────────────────
            elif msg_type == "audio_ended":
                session.music_playing = False
                if session.music_active and session.music_query:
                    session.autoplay_count += 1
                    suffix = _AUTOPLAY_SUFFIXES[session.autoplay_count % len(_AUTOPLAY_SUFFIXES)]
                    url, title = await _resolve_music_url(session.music_query + suffix)
                    if url:
                        session.music_playing = True
                        session.music_title   = title
                        _set_state(username, music_playing=True, current_song=title)
                        await websocket.send_json({
                            "type": "client_actions",
                            "actions": [{"type": "play_audio", "url": url, "title": title}],
                        })
                    else:
                        session.music_active = False
                        _set_state(username, music_playing=False, current_song="")

            # ── Music state sync from client ─────────────────────────────────
            elif msg_type == "music_state":
                session.music_playing = msg.get("playing", session.music_playing)
                session.music_paused  = msg.get("paused",  session.music_paused)
                _set_state(username,
                           music_playing=session.music_playing,
                           music_paused=session.music_paused)

            # ── Music control from dashboard HTTP API ────────────────────────
            elif msg_type == "music_control":
                cmd = msg.get("command", "")
                # Translate control commands into client actions
                ctl_map = {
                    "skip":   "skip",
                    "pause":  "pause_audio",
                    "resume": "resume_audio",
                    "stop":   "stop_audio",
                }
                if cmd in ctl_map:
                    if cmd == "skip" and session.music_query:
                        session.autoplay_count += 1
                        suffix = _AUTOPLAY_SUFFIXES[session.autoplay_count % len(_AUTOPLAY_SUFFIXES)]
                        url, title = await _resolve_music_url(session.music_query + suffix)
                        if url:
                            session.music_title = title
                            _set_state(username, current_song=title)
                            await websocket.send_json({
                                "type": "client_actions",
                                "actions": [{"type": "play_audio", "url": url, "title": title}],
                            })
                    else:
                        await websocket.send_json({
                            "type": "client_actions",
                            "actions": [{"type": ctl_map[cmd]}],
                        })
                elif cmd == "prev":
                    n = int(msg.get("n", 1))
                    if session.song_history:
                        idx = max(0, len(session.song_history) - n)
                        url, title = await _resolve_music_url(session.song_history[idx])
                        if url:
                            session.music_title  = title
                            session.music_query  = session.song_history[idx]
                            session.autoplay_count = 0
                            _set_state(username, current_song=title)
                            await websocket.send_json({
                                "type": "client_actions",
                                "actions": [{"type": "play_audio", "url": url, "title": title}],
                            })
                elif cmd == "replay" and session.music_query:
                    url, title = await _resolve_music_url(session.music_query)
                    if url:
                        session.music_title    = title
                        session.autoplay_count = 0
                        _set_state(username, current_song=title)
                        await websocket.send_json({
                            "type": "client_actions",
                            "actions": [{"type": "play_audio", "url": url, "title": title}],
                        })

            # ── Voice command ────────────────────────────────────────────────
            elif msg_type == "command":
                audio_b64 = msg.get("audio", "")
                mime      = msg.get("mime", "audio/webm")

                _set_state(username, state="activated")
                await websocket.send_json({"type": "state", "state": "activated"})

                if not audio_b64:
                    await websocket.send_json({"type": "error", "message": "No audio received."})
                    _set_state(username, state="idle")
                    await websocket.send_json({"type": "state", "state": "idle"})
                    continue

                _set_state(username, state="thinking")
                await websocket.send_json({"type": "state", "state": "thinking"})

                transcript = await _transcribe(base64.b64decode(audio_b64), mime)

                if not transcript:
                    await websocket.send_json({"type": "error", "message": "Could not understand audio."})
                    _set_state(username, state="idle")
                    await websocket.send_json({"type": "state", "state": "idle"})
                    continue

                print(f'  [{username}] heard: "{transcript}"')
                _set_state(username, transcript=transcript)
                await websocket.send_json({"type": "transcript", "text": transcript})

                response = await _ask_claude(session, transcript)
                mode     = response.get("mode", "none")

                if mode == "action":
                    replies, client_actions = [], []
                    chain_ctx = {}
                    for action in response.get("actions", []):
                        r, ca = await _execute_action(
                            action, session.workspaces, username,
                            session, websocket, chain_ctx
                        )
                        if r:
                            replies.append(r)
                        client_actions.extend(ca)
                    reply_text = " ".join(replies) or "Done."
                    _set_state(username, state="speaking")
                    await websocket.send_json({
                        "type": "response",
                        "text": reply_text,
                        "client_actions": client_actions,
                    })

                elif mode == "chat":
                    reply_text = response.get("reply", "I'm not sure how to answer that.")
                    _set_state(username, state="speaking")
                    await websocket.send_json({
                        "type": "response",
                        "text": reply_text,
                        "client_actions": [],
                    })

                else:
                    _set_state(username, state="idle")
                    await websocket.send_json({"type": "state", "state": "idle"})
                    continue

                _set_state(username, state="idle")
                await websocket.send_json({"type": "state", "state": "idle"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"  WS error [{session.username if session else '?'}]: {e}")
    finally:
        if session:
            async with _sessions_lock:
                sessions.pop(session.id, None)
            if session.overrode_sleep:
                _sleep_overrides = max(0, _sleep_overrides - 1)
                if _sleep_overrides == 0 and _is_sleep_time():
                    _sleep_mode = True
                    print("  Sleep mode re-activated.")
                    await _broadcast_sleep()
            if session.music_active:
                _set_state(session.username, music_playing=False, music_paused=False, current_song="")
            print(f"  [{session.username}] disconnected — {len(sessions)} remaining")

# ── Serve React dashboard (must be registered LAST) ───────────────────────────
if REACT_BUILD.exists():
    static_dir = REACT_BUILD / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="react-static")

    @app.get("/{path:path}")
    async def serve_react(path: str):
        candidate = REACT_BUILD / path
        if candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(REACT_BUILD / "index.html")
else:
    @app.get("/{path:path}")
    async def serve_no_build(path: str):
        return JSONResponse({
            "error": "React dashboard not built.",
            "hint": "cd jarvis-dashboard && npm install && npm run build",
        }, 503)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 54)
    print("  Jarvis Pi Server")
    print(f"  Listening on {SERVER_HOST}:{SERVER_PORT}")
    print(f"  Sleep window: {SLEEP_START}:00 – {SLEEP_END}:00")
    print(f"  Dashboard:    http://<pi-ip>:{SERVER_PORT}/")
    print("=" * 54)
    uvicorn.run(
        app, host=SERVER_HOST, port=SERVER_PORT,
        log_level="warning",
        ssl_keyfile=str(BASE_DIR / "key.pem")  if (BASE_DIR / "key.pem").exists()  else None,
        ssl_certfile=str(BASE_DIR / "cert.pem") if (BASE_DIR / "cert.pem").exists() else None,
    )
