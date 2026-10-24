"""
PC actions from the brain (Mobile Jarvis Phase 3, M3).

On the headless brain (the Pi), actions that need the PC itself (registry.PC_ONLY: apps,
workspaces, URLs, files, music, screenshots, the agent) are run on the PC client:

  run(action) → "do" event {id, action, source} → the PC runs the handler → POST
  /api/client/result {id, ok, result, chain} (a screenshot comes along as an image for the Stage)

While it runs, what the handler says comes back line by line (POST /api/client/say) and is
said here, so it reaches whoever asked: a phone command that opens Discord answers on the
phone. When the handler needs an answer (agent.confirm, "which numbers?") and the command
didn't come from the PC, it asks through here (POST /api/client/ask) and the answer comes from
the phone. Commands said at the PC are spoken and answered there directly.

PC off → "Your PC is off. Want me to do that when it's back?" → yes → jarvis_memory/pc_queue.json,
run when the PC client connects ("While you were away you asked me to open Discord. Done.").
"""

import itertools
import json
import os
import threading
import time

from . import config, stage, turns
from .tts import speak

_QUEUE_FILE = os.path.join(config.MEMORY_DIR, "pc_queue.json")
_ids = itertools.count(1)
_pending: dict = {}          # do id -> {"done": Event, "result": {...}, "said": int, "source"}
_lock = threading.Lock()     # one PC action at a time (the queue runs in its own thread)
_NOT_LATER = {"screenshot"}  # only makes sense right now
_TIMEOUT = {"agent": 900, "find_files": 180, "file": 180, "screenshot": 90}


def describe(a):
    """'open Discord': what the action does, for "Want me to ... when it's back?"."""
    t, target = a.get("type"), a.get("target") or a.get("query") or a.get("keyword") or ""
    if t == "app":
        return f"open {target}"
    if t == "workspace":
        return f"open the {target} workspace"
    if t in ("url", "bookmark"):
        return f"open {target}"
    if t == "search":
        return f"search {a.get('engine', 'Google')} for {target}"
    if t in ("find_files", "file"):
        return f"open the file {target}"
    if t == "vscode":
        return "open VS Code"
    if t == "music":
        return f"play {target}" if a.get("command", "play") == "play" else f"{a.get('command')} the music"
    if t == "agent":
        return a.get("task", "do that")[:120]
    return "do that"


# ── Running an action on the PC ───────────────────────────────────────────────
def run(a, chain, source=None):
    """Run a PC-only action on the PC client (called from actions.handle_response). source:
    where the command came from (default: the current turn's)."""
    if not turns.pc_online():
        return _offer(a)
    did = next(_ids)
    source = source or turns.current["source"]
    job = {"done": threading.Event(), "result": None, "said": 0, "source": source}
    with _lock:
        _pending[did] = job
        turns.publish({"type": "do", "id": did, "action": a, "source": source})
        print(f"  [pc] {a.get('type')} → PC (waiting)")
        if not job["done"].wait(_TIMEOUT.get(a.get("type"), 60)):
            _pending.pop(did, None)
            speak("Your PC didn't answer, sir. It may have gone to sleep.")
            return f"PC timeout: {a.get('type')}"
        _pending.pop(did, None)
    r = job["result"] or {}
    if r.get("listened"):            # its question was answered at the PC: don't ask it again
        from . import audio
        audio.last_listen[0] = time.time()
    for k, v in (r.get("chain") or {}).items():     # e.g. a screenshot's analysis for generate_file
        chain.setdefault(k, v)
    result = str(r.get("result") or "")
    if not r.get("ok"):
        speak("That didn't work on your PC.")
    elif not job["said"] and source != "pc" and result:
        speak(f"{result} on your PC.")                # nothing said: confirm it to the phone
    return f"PC: {result}"


def said(did, text, spoken):
    """A line the handler said on the PC. spoken: the PC already said it aloud (a command said
    at the PC), so it's only logged here; otherwise it's said here, for the turn's source."""
    job = _pending.get(did)
    if not job or not text:
        return False
    job["said"] += 1
    if spoken:
        turns.echo(text)
        if text.rstrip().endswith("?"):           # it asked something: the brain listens for the answer
            from . import tts
            tts.last_question.update(text=text.strip(), time=time.time())
    else:
        speak(text)
    return True


def ask(did):
    """The handler wants an answer (the command came from the phone): listen for it there."""
    from . import audio
    from .tts import wait_tts
    if did not in _pending:
        return ""
    wait_tts()
    return audio.listen_for_command()


def result(did, payload, image=None):
    job = _pending.get(did)
    if not job:
        return False
    if image:                        # a screenshot: onto the Stage here
        stage.add_image(image, caption=str(payload.get("result") or ""), title="Screenshot")
    job["result"] = payload
    job["done"].set()
    return True


# ── PC off: offer to do it later ──────────────────────────────────────────────
def _offer(a):
    from . import audio
    from .agent import _NO, _YES
    from .tts import wait_tts
    what = describe(a)
    if a.get("type") in _NOT_LATER:
        speak("Your PC is off, sir, so I can't see its screen right now.")
        return "PC off"
    speak(f"Your PC is off, sir. Want me to {what} when it's back on?")
    wait_tts()
    answer = audio.listen_for_command()
    if not (_YES.search(answer or "") and not _NO.search(answer or "")):
        speak("Alright, I'll leave it.")
        return "PC off: not queued"
    queue = _load()
    queue.append({"action": a, "what": what, "time": time.time()})
    _save(queue)
    speak("Will do. I'll take care of it when your PC is on.")
    return f"PC off: queued {what}"


def _load():
    try:
        with open(_QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save(queue):
    os.makedirs(config.MEMORY_DIR, exist_ok=True)
    with open(_QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=1)


def _flush():
    """The PC is back: run what was asked while it was off, then say so (at the PC)."""
    queue = _load()
    if not queue:
        return
    _save([])
    time.sleep(3)                    # let the client settle (its greeting, the event stream)
    done = []
    with turns.speaking_as("pc"):
        for item in queue:
            print(f"  [pc] running queued: {item['what']}")
            run(item["action"], {}, source="pc")
            done.append(item["what"])
        speak("While you were away you asked me to " + " and ".join(done) + ". Done.")


def _on_pc_online():
    threading.Thread(target=_flush, daemon=True, name="pc-queue").start()


turns.on_pc_online.append(_on_pc_online)
