"""
Phone notifications through ntfy (https://ntfy.sh): install the ntfy app and subscribe
to the topic Jarvis prints at startup. Sending is one HTTP request, no account needed.

- Reminders go to the phone when you're away from the PC (no keyboard/mouse input and no
  voice command for config.PHONE_AWAY_MIN minutes), and deadlines close to due always do.
- Reminder notifications carry "Mark done" / "Snooze 1h" buttons. The ntfy app calls
  Jarvis back over Tailscale (POST /api/notify/action), so they work wherever the phone is
  on your tailnet. Each call must carry the secret key from jarvis_memory/notify.json.
- "Send that to my phone" pushes any text or link (send_to_phone action).

The topic name is random and acts as a password: anyone who knows it can read your
notifications on ntfy.sh. Set NTFY_SERVER to a self-hosted ntfy to keep them private.
"""

import ctypes
import json
import os
import secrets
import threading
import time

import requests

from . import config, state
from .registry import action
from .tts import speak

_FILE = os.path.join(config.MEMORY_DIR, "notify.json")
_settings = {}                 # {"topic", "key"}
_last_voice = [0.0]            # when the user last gave a command


def _load():
    global _settings
    try:
        with open(_FILE, encoding="utf-8") as f:
            _settings = json.load(f)
    except (OSError, ValueError):
        _settings = {}
    if not _settings.get("topic") or not _settings.get("key"):
        _settings = {"topic": f"jarvis-{secrets.token_urlsafe(12).replace('_', '').replace('-', '')[:14].lower()}",
                     "key": secrets.token_urlsafe(24)}
        os.makedirs(config.MEMORY_DIR, exist_ok=True)
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(_settings, f, indent=1)


def topic():
    return config.NTFY_TOPIC or _settings.get("topic", "")


def check_key(key):
    return bool(key) and secrets.compare_digest(str(key), _settings.get("key", ""))


# ── Are you at the PC? ────────────────────────────────────────────────────────
class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """Seconds since the last keyboard or mouse input (Windows)."""
    try:
        info = _LastInput()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000
    except (AttributeError, OSError):
        return 0.0


def away():
    """True when nobody has touched the PC or spoken to Jarvis for PHONE_AWAY_MIN minutes."""
    limit = config.PHONE_AWAY_MIN * 60
    return idle_seconds() > limit and time.time() - _last_voice[0] > limit


def _on_log(role, text):
    if role == "user":
        _last_voice[0] = time.time()


# ── Sending ───────────────────────────────────────────────────────────────────
def _callback_url():
    """Where the phone can reach Jarvis: the Tailscale address (None without Tailscale)."""
    url = state.dash_url or ""
    return url if url and "localhost" not in url and "127.0.0.1" not in url else None


def send(title, message, tags=(), priority=3, actions=(), click=""):
    """Push a notification. Runs in the background; returns immediately."""
    if not config.PHONE_PUSH or not topic():
        return False
    body = {"topic": topic(), "title": title[:120], "message": message[:3800], "tags": list(tags),
            "priority": priority}
    if actions:
        body["actions"] = list(actions)[:3]
    if click:
        body["click"] = click

    def post():
        try:
            r = requests.post(config.NTFY_SERVER.rstrip("/"), json=body, timeout=10)
            r.raise_for_status()
            print(f"  [phone] sent: {title}")
        except Exception as e:
            print(f"  [phone] could not send ({e})")

    threading.Thread(target=post, daemon=True).start()
    return True


def button(label, payload):
    base = _callback_url()
    if not base:
        return None
    return {"action": "http", "label": label, "url": f"{base}/api/notify/action", "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"key": _settings.get("key", ""), **payload}), "clear": True}


def reminder(message, item, occ_key=None):
    """A reminder with Mark done / Snooze buttons (when the phone can reach Jarvis)."""
    actions = []
    if item.get("kind") != "timer" and occ_key:
        actions = [b for b in (button("Mark done", {"do": "done", "item": item["id"], "occ": occ_key}),
                               button("Snooze 1h", {"do": "snooze", "item": item["id"], "occ": occ_key}))
                   if b]
    title = "Timer" if item.get("kind") == "timer" else ("Due soon" if item.get("kind") == "deadline" else "Reminder")
    text = message.replace("Reminder, sir: ", "").replace("Time's up: ", "")
    return send(title, text, tags=["alarm_clock"] if item.get("kind") == "timer" else ["bell"],
                priority=4, actions=actions)


def handle_action(payload):
    """A button tapped on the phone. Returns (ok, message)."""
    from . import agenda, coding
    if not check_key(payload.get("key")):
        return False, "bad key"
    if payload.get("do") == "phone":
        name, ok = coding.open_on_phone(payload.get("project"))
        if name and not ok:
            send("Trust needed", f"Claude Code hasn't been trusted in {name} yet, so it's waiting for you to accept "
                 "the folder prompt on your PC. After that, it opens straight into the Claude app.", tags=["warning"])
        return (True, f"Opened {name} for Remote Control") if name else (False, "nothing to open")
    if payload.get("do") == "approve":
        threading.Thread(target=coding.approve, args=(payload.get("project"),), daemon=True).start()
        return True, f"Allowed the refused commands in {payload.get('project')}"
    item = agenda.get(payload.get("item", ""))
    if not item:
        return False, "no such item"
    if payload.get("do") == "done":
        agenda.mark(item["id"], "done", payload.get("occ", ""))
        return True, f"Marked {item['text']} done"
    if payload.get("do") == "snooze":
        agenda.snooze(item["id"], payload.get("occ", ""), 60)
        return True, f"Snoozed {item['text']} for an hour"
    return False, "unknown action"


def start():
    _load()
    state.on_log.append(_on_log)
    return topic()


# ── Voice ─────────────────────────────────────────────────────────────────────
@action("send_to_phone",
        schema='{"type":"send_to_phone","title":"<short title>","text":"<the full content to send, written out — e.g. the list of places, the answer, the steps>","url":"<a link to open, or empty>"}',
        rules=['"send that to my phone", "text me the list", "put that on my phone", "notify me with X" → type "send_to_phone"; write the actual content out in "text" from the conversation (names, addresses, links), not a reference to it'])
def _send_to_phone(a, chain):
    text = (a.get("text") or "").strip()
    if not text:
        speak("What should I send?")
        return "Phone: nothing to send"
    if not config.PHONE_PUSH:
        speak("Phone notifications are turned off in my settings.")
        return "Phone: disabled"
    send(a.get("title") or "From Jarvis", text, tags=["iphone"], click=a.get("url") or "")
    speak("Sent to your phone.")
    return f"Phone: {a.get('title') or text[:40]}"
