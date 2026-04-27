#!/usr/bin/env python3
"""
Jarvis Pi Server — multi-session remote access via WebSocket.
Run on the Pi: python jarvis_server.py

Each browser tab gets its own session with isolated CHAT_HISTORY.
Memory is stored per-user in users/<username>/memory.json — never shared between users.
Sleep mode: 10pm–6am. Users can override by tapping "Wake Jarvis".
After the last override session disconnects, sleep re-activates automatically.

Dependencies (Pi):
    pip install fastapi uvicorn[standard] anthropic python-dotenv faster-whisper requests
    sudo apt install ffmpeg

Windows clients (jarvis.py) sync memory via the /api/memory/<username> REST endpoints.
"""

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from faster_whisper import WhisperModel

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BRAVE_API_KEY     = os.environ.get("BRAVE_API_KEY", "")
SERVER_HOST       = os.environ.get("JARVIS_HOST", "0.0.0.0")
SERVER_PORT       = int(os.environ.get("JARVIS_PORT", "5150"))
SLEEP_START       = 22  # 10 pm
SLEEP_END         = 6   # 6 am

CLIENT_HTML = Path(__file__).parent / "jarvis_client.html"

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

# ── Shared AI client ───────────────────────────────────────────────────────────
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Whisper — one model, one lock, shared across all sessions ──────────────────
print("  Loading Whisper model...")
WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
_whisper_lock = threading.Lock()
print("  Whisper ready.")

# ── Sleep mode ─────────────────────────────────────────────────────────────────
_sleep_mode      = False
_sleep_overrides = 0   # count of sessions that have explicitly woken Jarvis


def _is_sleep_time() -> bool:
    h = datetime.now().hour
    return h >= SLEEP_START or h < SLEEP_END


# ── Session ────────────────────────────────────────────────────────────────────
@dataclass
class Session:
    id: str
    username: str
    websocket: WebSocket
    overrode_sleep: bool = False
    chat_history: list = field(default_factory=list)
    error_count: int = 0


sessions: dict[str, Session] = {}
_sessions_lock = asyncio.Lock()


# ── System prompt (remote / browser sessions) ──────────────────────────────────
def _build_system_prompt(username: str) -> str:
    return (
        "You are JARVIS, a voice assistant. "
        "Return ONLY a single raw JSON object — no explanation, no markdown fences.\n"
        "\nACTION TYPES (choose exactly one):\n"
        '{"mode":"action","actions":[{"type":"url","target":"<full url>"}]}\n'
        '{"mode":"action","actions":[{"type":"search","engine":"google|youtube|github","query":"<q>"}]}\n'
        '{"mode":"action","actions":[{"type":"web_search","query":"<search query>"}]}\n'
        '{"mode":"action","actions":[{"type":"generate_file","filename":"<name.ext>","prompt":"<desc>","search_query":"<or empty string>"}]}\n'
        '{"mode":"chat","reply":"<spoken answer>"}\n'
        '{"mode":"none"}\n\n'
        "RULES:\n"
        '- "open [site]" / "go to [url]" → type "url"\n'
        '- "search [engine] for [q]" → type "search"\n'
        '- Real-time info needed (news, weather, sports, prices) → type "web_search"\n'
        '- "write/generate/create [file]" → type "generate_file"; always include a file extension\n'
        '- Questions / conversation → mode "chat"; 1–2 spoken sentences, no markdown\n'
        '- Filler / unclear → mode "none"\n'
        f"- The current user is {username}.\n"
    )


def _init_history(username: str) -> list[dict]:
    return [{"role": "system", "content": _build_system_prompt(username)}]


# ── Brave search ───────────────────────────────────────────────────────────────
def _brave_search(query: str, count: int = 5) -> list[dict]:
    if not BRAVE_API_KEY:
        return []
    try:
        r = requests.get(
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
    snippets = "\n".join(f"{i+1}. {r['title']}: {r['description']}" for i, r in enumerate(results))
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="You are a voice assistant. Summarise the results as 2–3 spoken sentences. No markdown.",
            messages=[{"role": "user", "content": f"Q: {query}\n\nResults:\n{snippets}"}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Search failed: {e}"


# ── File generation ────────────────────────────────────────────────────────────
def _generate_file(prompt: str, filename: str, context: str = "") -> str:
    ext  = os.path.splitext(filename)[1].lower()
    lang = {".py": "Python", ".js": "JavaScript", ".html": "HTML", ".md": "Markdown",
            ".txt": "plain text", ".json": "JSON", ".sh": "Shell"}.get(ext, "plain text")
    body = f"Real-time data:\n{context}\n\nTask: {prompt}" if context else prompt
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            system=f"Output ONLY raw file content — no explanation, no markdown fences. File: '{filename}' ({lang}).",
            messages=[{"role": "user", "content": body}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"# Generation failed: {e}"


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


# ── Claude — per-session, isolated history ─────────────────────────────────────
_BYPASS = {
    "never mind", "nevermind", "cancel", "stop", "nothing", "forget it",
    "um", "uh", "hmm", "okay", "ok", "yeah", "yes", "thanks", "cool", "got it",
}


async def _ask_claude(session: Session, command: str) -> dict:
    if command.strip().lower().rstrip(".,!?") in _BYPASS:
        return {"mode": "none"}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_ask_claude, session, command)


def _sync_ask_claude(session: Session, command: str) -> dict:
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

        result = _parse_json(raw)
        if result:
            session.error_count = 0
            session.chat_history.append({"role": "assistant", "content": raw})
            if len(session.chat_history) > 13:
                session.chat_history = session.chat_history[:1] + session.chat_history[-12:]
            return result

        session.error_count += 1
        session.chat_history = snapshot
        if session.error_count >= 3:
            session.chat_history = _init_history(session.username)
            session.error_count = 0
        return {"mode": "none"}

    except Exception as e:
        session.chat_history = snapshot
        print(f"  Claude error [{session.username}]: {e}")
        return {"mode": "none"}


def _parse_json(text: str) -> Optional[dict]:
    for attempt in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        try:
            return json.loads(attempt)
        except Exception:
            pass
    return None


# ── Action execution ───────────────────────────────────────────────────────────
async def _execute_action(action: dict) -> tuple[str, list[dict]]:
    """Returns (spoken_reply, list_of_client_side_actions).
    Client-side actions are forwarded to the browser (e.g. open a URL, trigger download)."""
    kind   = action.get("type", "")
    target = action.get("target", "")
    query  = action.get("query", "")

    if kind == "url":
        return f"Opening {target}.", [{"type": "open_url", "url": target}]

    elif kind == "search":
        engine = action.get("engine", "google")
        urls = {
            "google":  f"https://google.com/search?q={query.replace(' ', '+')}",
            "youtube": f"https://youtube.com/results?search_query={query.replace(' ', '+')}",
            "github":  f"https://github.com/search?q={query.replace(' ', '+')}",
        }
        return f"Searching {engine}.", [{"type": "open_url", "url": urls.get(engine, urls["google"])}]

    elif kind == "web_search":
        if not BRAVE_API_KEY:
            return "Web search isn't configured on this server.", []
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _brave_search, query or target)
        answer  = await loop.run_in_executor(None, _synthesize_answer, query or target, results)
        return answer, []

    elif kind == "generate_file":
        filename = action.get("filename", "output.txt")
        prompt   = action.get("prompt", "")
        search_q = action.get("search_query", "")
        loop = asyncio.get_event_loop()
        context = ""
        if search_q and BRAVE_API_KEY:
            results = await loop.run_in_executor(None, _brave_search, search_q, 5)
            context = "\n".join(f"{r['title']}: {r['description']}" for r in results)
        content = await loop.run_in_executor(None, _generate_file, prompt, filename, context)
        return f"{filename} is ready.", [{"type": "download_file", "filename": filename, "content": content}]

    return "", []


# ── Sleep mode ─────────────────────────────────────────────────────────────────
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
            print("  Sleep mode activated (scheduled).")
            await _broadcast_sleep()
        elif not in_window and _sleep_mode:
            _sleep_mode = False
            print("  Sleep mode deactivated (scheduled).")


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI()


@app.on_event("startup")
async def _on_startup():
    global _sleep_mode
    asyncio.create_task(_sleep_scheduler())
    if _is_sleep_time():
        _sleep_mode = True
        print("  Started in sleep mode (outside active hours).")


@app.get("/")
async def _serve_client():
    if not CLIENT_HTML.exists():
        return JSONResponse({"error": "jarvis_client.html not found next to jarvis_server.py"}, status_code=404)
    return FileResponse(CLIENT_HTML)


# ── WebSocket endpoint ─────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    global _sleep_overrides
    await websocket.accept()
    session: Optional[Session] = None

    try:
        # ── Auth phase ────────────────────────────────────────────────────────
        try:
            auth = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        except asyncio.TimeoutError:
            await websocket.close(code=4001, reason="auth timeout")
            return

        if auth.get("type") != "auth":
            await websocket.close(code=4002, reason="expected auth message first")
            return

        username = auth.get("username", "").strip().lower()

        if not username:
            await websocket.send_json({"type": "error", "message": "Username cannot be empty."})
            await websocket.close(code=4003)
            return

        # ── Sleep gate ────────────────────────────────────────────────────────
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

        # ── Create session ────────────────────────────────────────────────────
        session = Session(
            id=str(uuid.uuid4()),
            username=username,
            websocket=websocket,
            overrode_sleep=overrode,
            chat_history=_init_history(username),
        )
        async with _sessions_lock:
            sessions[session.id] = session

        print(f"  [{username}] connected ({session.id[:8]}) — {len(sessions)} active session(s)")

        await websocket.send_json({
            "type": "auth_ok",
            "username": username,
        })

        # ── Command loop ──────────────────────────────────────────────────────
        while True:
            try:
                msg = await websocket.receive_json()
            except Exception:
                break

            msg_type = msg.get("type")

            if msg_type == "disconnect":
                break

            elif msg_type == "wake":
                # Mid-session wake (e.g. scheduler activated sleep while connected)
                if _is_sleep_time() and not session.overrode_sleep:
                    _sleep_mode = False
                    _sleep_overrides += 1
                    session.overrode_sleep = True
                    print(f"  Mid-session wake by {username}.")
                await websocket.send_json({"type": "awake", "message": "I'm awake. What do you need?"})

            elif msg_type == "command":
                audio_b64 = msg.get("audio", "")
                mime      = msg.get("mime", "audio/webm")

                await websocket.send_json({"type": "state", "state": "transcribing"})

                if not audio_b64:
                    await websocket.send_json({"type": "error", "message": "No audio received."})
                    await websocket.send_json({"type": "state", "state": "idle"})
                    continue

                transcript = await _transcribe(base64.b64decode(audio_b64), mime)

                if not transcript:
                    await websocket.send_json({"type": "error", "message": "Could not understand audio."})
                    await websocket.send_json({"type": "state", "state": "idle"})
                    continue

                print(f'  [{username}] heard: "{transcript}"')
                await websocket.send_json({"type": "transcript", "text": transcript})
                await websocket.send_json({"type": "state", "state": "thinking"})

                response = await _ask_claude(session, transcript)
                mode = response.get("mode", "none")

                if mode == "action":
                    replies, client_actions = [], []
                    for action in response.get("actions", []):
                        r, ca = await _execute_action(action)
                        if r:
                            replies.append(r)
                        client_actions.extend(ca)
                    await websocket.send_json({
                        "type": "response",
                        "text": " ".join(replies),
                        "client_actions": client_actions,
                    })

                elif mode == "chat":
                    reply = response.get("reply", "I'm not sure how to answer that.")
                    await websocket.send_json({"type": "response", "text": reply, "client_actions": []})

                else:  # "none"
                    await websocket.send_json({"type": "state", "state": "idle"})
                    continue

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
                    print("  Sleep mode re-activated (last override session ended).")
                    await _broadcast_sleep()
            print(f"  [{session.username}] disconnected — {len(sessions)} session(s) remaining")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 52)
    print("  Jarvis Pi Server")
    print(f"  Listening on {SERVER_HOST}:{SERVER_PORT}")
    print(f"  Sleep window: {SLEEP_START}:00 – {SLEEP_END}:00")
    print(f"  Client page:  http://<pi-ip>:{SERVER_PORT}/")
    print("=" * 52)
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
