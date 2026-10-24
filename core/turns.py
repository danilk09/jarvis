"""
Turns and clients (Mobile Jarvis Phase 3, M1).

Every command arrives as a turn with a source: "phone" (the /phone page), "pc" (the wake
word on this PC, later the PC client), "device". The main loop takes turns from `queue`
(besides the wake word), and while one is being handled `current` says where it came from,
so whatever Jarvis says is tagged with it: the phone speaks its own replies and anything
Jarvis says on his own between turns (Claude Code finished, a reminder), not the PC's.

Clients follow along on GET /api/client/events (SSE), one event per change:
  {"type": "line", "id", "text", "source", "time"}   Jarvis said something; audio at /api/say/<id>
  {"type": "user", "text", "source", "time"}          a command (or answer) was heard
  {"type": "state", "state"}                          idle | activated | thinking | speaking
  {"type": "listen", "question", "until", "source"}   Jarvis waits for an answer from `source`
  {"type": "listen_end"}
A client that (re)connects gets a "hello" with the current state and any open listen, and
reads earlier lines from /status's log.
"""

import collections
import itertools
import queue as _queue
import threading
import time

from . import state

queue = _queue.Queue()     # turns for the main loop: {"text", "source", "image"?}

current = {"id": 0, "source": None}   # the turn being handled (source None = between turns)
_ids = itertools.count(1)
_lock = threading.Lock()
_subs: list = []
_lines = collections.OrderedDict()    # line id -> {"text", "source", "time"}, for /api/say
_LINES_MAX = 60
_listen = {"open": None}              # the open listen event, sent to clients that connect mid-wait


def put(text, source="phone", image=None):
    """Queue a command from a client."""
    turn = {"text": text, "source": source}
    if image:
        turn["image"] = image
    queue.put(turn)


def begin(source):
    """The main loop started handling a turn from `source`."""
    current.update(id=next(_ids), source=source)


def end():
    current.update(source=None)


# ── Event stream ──────────────────────────────────────────────────────────────
def subscribe():
    q = _queue.Queue(maxsize=500)
    with _lock:
        _subs.append(q)
    hello = {"type": "hello", "state": state.dash_state.get("state", "idle"), "listen": _listen["open"]}
    q.put(hello)
    return q


def unsubscribe(q):
    with _lock:
        if q in _subs:
            _subs.remove(q)


def publish(event):
    with _lock:
        subs = list(_subs)
    for q in subs:
        try:
            q.put_nowait(event)
        except _queue.Full:        # a client that stopped reading: drop it, it reconnects
            unsubscribe(q)


def line(line_id):
    with _lock:
        return _lines.get(line_id)


def _on_log(role, text):
    now = time.time()
    if role == "jarvis":
        lid = next(_ids)
        entry = {"text": text, "source": current["source"], "time": now}
        with _lock:
            _lines[lid] = entry
            while len(_lines) > _LINES_MAX:
                _lines.popitem(last=False)
        publish({"type": "line", "id": lid, **entry})
    else:
        publish({"type": "user", "text": text, "source": current["source"], "time": now})


def _on_state(new_state):
    publish({"type": "state", "state": new_state})


def listening(question, seconds):
    """Jarvis waits (up to `seconds`) for the current turn's source to answer `question`."""
    _listen["open"] = {"type": "listen", "question": question, "until": time.time() + seconds,
                       "source": current["source"]}
    publish(_listen["open"])


def listen_done():
    if _listen["open"]:
        _listen["open"] = None
        publish({"type": "listen_end"})


state.on_log.append(_on_log)
state.on_state.append(_on_state)
