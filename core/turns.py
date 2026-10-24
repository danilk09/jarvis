"""
Turns and clients (Mobile Jarvis Phase 3, M1).

Every command arrives as a turn with a source: "phone" (the /phone page), "pc" (the wake
word on this PC, later the PC client), "device". The main loop takes turns from `queue`
(besides the wake word), and while one is being handled `current` says where it came from,
so whatever Jarvis says is tagged with it: the phone speaks its own replies and anything
Jarvis says on his own between turns (Claude Code finished, a reminder), not the PC's.

Clients follow along on GET /api/client/events (SSE), one event per change:
  {"type": "line", "id", "text", "source", "to", "time"}   Jarvis said something, for the clients
                                                     in "to" (targets()); audio at /api/say/<id>
  {"type": "user", "text", "source", "time"}          a command (or answer) was heard
  {"type": "state", "state"}                          idle | activated | thinking | speaking
  {"type": "listen", "question", "until", "source", "to"}   Jarvis waits for an answer from "to"
  {"type": "listen_end"}
A client that (re)connects gets a "hello" with the current state and any open listen, and
reads earlier lines from /status's log.
"""

import collections
import contextlib
import itertools
import queue as _queue
import threading
import time

from . import config, state

queue = _queue.Queue()     # turns for the main loop: {"text", "source", "image"?}

current = {"id": 0, "source": None}   # the turn being handled (source None = between turns)
_ids = itertools.count(1)
_lock = threading.Lock()
_subs: list = []
_lines = collections.OrderedDict()    # line id -> {"text", "source", "time"}, for /api/say
_LINES_MAX = 60
_listen = {"open": None}              # the open listen event, sent to clients that connect mid-wait
_local = threading.local()            # per thread: speaking_as() source, echo() in progress
on_pc_online: list = []               # called when the PC client connects (pc.py runs its queue)


def put(text, source="phone", image=None, fresh=False):
    """Queue a command from a client. fresh: a new command (the PC heard the wake word), never
    the answer to a question Jarvis is waiting on — that wait ends and this runs as its own turn."""
    turn = {"text": text, "source": source}
    if image:
        turn["image"] = image
    if fresh:
        turn["fresh"] = True
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


@contextlib.contextmanager
def speaking_as(source):
    """Lines said by this thread belong to `source` (pc.py's queue, run outside any turn)."""
    _local.source = source
    try:
        yield
    finally:
        _local.source = None


def echo(text):
    """Log a line the PC has already said aloud: shown everywhere, spoken by no one."""
    _local.echo = True
    try:
        state.push_log("jarvis", text)
    finally:
        _local.echo = False


def line_source():
    """Where the line being logged now belongs: speaking_as() on this thread, else the turn's."""
    return getattr(_local, "source", None) or current["source"]


def _on_log(role, text):
    now = time.time()
    source = line_source()
    if role == "jarvis":
        lid = next(_ids)
        entry = {"text": text, "source": source, "time": now, "to": targets(source)}
        if getattr(_local, "echo", False):
            entry["spoken"] = True
        with _lock:
            _lines[lid] = entry
            while len(_lines) > _LINES_MAX:
                _lines.popitem(last=False)
        publish({"type": "line", "id": lid, **entry})
    else:
        publish({"type": "user", "text": text, "source": source, "time": now})


def _on_state(new_state):
    publish({"type": "state", "state": new_state})


# ── The PC client's heartbeat ─────────────────────────────────────────────────
pc = {"seen": 0.0, "idle": None}     # last heartbeat, and seconds since your last keyboard/mouse input


def presence(idle):
    was = pc_online()
    pc.update(seen=time.time(), idle=idle)
    if not was:
        for fn in on_pc_online:
            try:
                fn()
            except Exception as e:
                print(f"  PC online hook error: {e}")


def pc_online():
    return time.time() - pc["seen"] < 45


def at_pc():
    """You're at the PC: its client is connected, it isn't locked, and you've used it (or talked
    to Jarvis there) in the last PHONE_AWAY_MIN minutes."""
    return pc_online() and pc["idle"] is not None and pc["idle"] < config.PHONE_AWAY_MIN * 60


def targets(source, question=False):
    """Who hears a line (M4): a PC command's replies at the PC; a phone command's on the phone,
    and at the PC too when you're there (a question only where it was asked); Jarvis's own
    lines (reminders, Claude Code finished, check-ins) at the PC if you're there, else the phone."""
    if source == "phone":
        return ["phone"] if question or not at_pc() else ["phone", "pc"]
    if source is None:
        return ["pc"] if at_pc() else ["phone"]
    return [source]


def listening(question, seconds):
    """Jarvis waits (up to `seconds`) for the current turn's source to answer `question`."""
    source = getattr(_local, "source", None) or current["source"]
    _listen["open"] = {"type": "listen", "question": question, "until": time.time() + seconds,
                       "source": source, "to": targets(source, question=True)}
    publish(_listen["open"])


def listen_done():
    if _listen["open"]:
        _listen["open"] = None
        publish({"type": "listen_end"})


state.on_log.append(_on_log)
state.on_state.append(_on_state)
