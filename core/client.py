"""
The PC as a client of the Pi brain (Mobile Jarvis Phase 3, M2).

    python jarvis.py --client https://jarvis-pi.<tailnet>.ts.net:5151   (or CLIENT_OF / JARVIS_BRAIN)

The PC keeps what needs it: the wake word, the mic, Whisper, voice ID, and Jarvis's voice on
these speakers (with the orb on the local dashboard). Everything else — routing, Claude,
memory, agenda, Claude Code — runs on the Pi, so there's one memory and one history.

  wake word → record → Whisper + voice ID here → POST {brain}/api/turn {text, source: "pc"}
  GET {brain}/api/client/events → "line" from a pc turn → spoken here
                                → "listen" for the pc → record the answer here → POST it back
  every 15 s → POST {brain}/api/client/presence (are you at the PC? phone notifications if not)

Saying "Jarvis" while he talks cuts him off here (the Pi has already said everything it had to
say); the new command is sent as "fresh", so a question the Pi was waiting on isn't answered
with it. PC actions (apps, workspaces, music...) come in M3: until then the Pi answers that
they need the PC.
"""

import json
import random
import threading
import time

import requests

from . import audio, config, music, notify, state, voice
from .tts import speak, suppressed, wait_tts
from . import tts

_ACKS = ["Yes sir.", "Yes sir.", "Sir.", "Go ahead, sir.", "Listening."]
_UNREACHABLE = "I can't reach the brain on the Pi right now."

_listen = {"question": None, "since": 0.0}   # a question the Pi is waiting for us to answer
_wake_listen = threading.Event()            # set when there's something for the main loop to do
_online = threading.Event()


def _url(path):
    return config.CLIENT_OF + path


def _post(path, body, timeout=10):
    try:
        r = requests.post(_url(path), json=body, timeout=timeout)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  [client] {path} failed: {e}")
        return False


# ── Events from the brain ─────────────────────────────────────────────────────
def _handle(ev):
    t = ev.get("type")
    if t == "line" and ev.get("source") == "pc":
        speak(ev["text"])
    elif t in ("listen", "hello") and (ev if t == "listen" else ev.get("listen") or {}).get("source") == "pc":
        q = ev if t == "listen" else ev["listen"]
        _listen.update(question=q.get("question") or "", since=time.time())
        _wake_listen.set()
    elif t == "listen_end":
        _listen["question"] = None
    elif t == "state":
        # The orb: thinking while the brain works on our turn; idle again once nothing is playing
        if ev["state"] == "thinking" and not tts.busy() and not state.listening.is_set():
            state.push_state("thinking")
        elif ev["state"] == "idle" and not tts.busy() and not state.listening.is_set():
            state.push_state("idle")


def _events():
    told = False
    while True:
        try:
            with requests.get(_url("/api/client/events"), stream=True, timeout=(5, 60)) as r:
                r.raise_for_status()
                if not _online.is_set():
                    print(f"  [client] connected to the brain at {config.CLIENT_OF}")
                _online.set()
                told = False
                for raw in r.iter_lines(decode_unicode=True):
                    if raw and raw.startswith("data: "):
                        try:
                            _handle(json.loads(raw[6:]))
                        except Exception as e:
                            print(f"  [client] event error: {e}")
        except requests.RequestException as e:
            if _online.is_set() or not told:
                print(f"  [client] lost the brain ({e.__class__.__name__}); retrying")
                told = True
            _online.clear()
        time.sleep(3)


def _presence():
    while True:
        if _online.is_set():
            _post("/api/client/presence", {"idle": round(notify.quiet_seconds())}, timeout=5)
        time.sleep(15)


# ── Turns ─────────────────────────────────────────────────────────────────────
def _send(command, fresh=True):
    notify._last_voice[0] = time.time()          # you just spoke: you're at the PC
    state.push_state("thinking", transcript=command)
    if not _post("/api/turn", {"text": command, "source": "pc", "fresh": fresh}):
        speak(_UNREACHABLE)


def _activation():
    """The wake word (or an orb click): get the command, check the voice, send it."""
    audio.disable_wake()
    audio.activated.clear()
    suppressed.clear()
    state.push_state("activated")
    print("\n  Activated!")
    command = audio.capture_followup()            # "Jarvis, what time is it" in one breath
    if command is None:
        state.push_state("idle")
        return                                    # false wake from background speech
    source = "wake"
    if not command:
        if voice.wake_is_stranger():
            state.push_state("idle")
            return
        speak(random.choice(_ACKS))
        wait_tts()
        command = audio.listen_for_command()
        source = "dashboard" if audio.wake_source() == "dashboard" else "prompted"
    command = voice.screen(command, source) if command else command
    if not command:
        state.push_state("idle")
        return
    _send(command)


def _answer():
    """The brain asked something at the PC: once it's been said, listen for the answer."""
    question = _listen["question"]
    wait_tts()
    if _listen["question"] is None or audio.activated.is_set():
        return                                    # it stopped waiting, or you said "Jarvis"
    audio.disable_wake()
    music.duck()
    print(f'  Listening for an answer to: "{question}"')
    heard = audio.listen_for_command(onset_timeout=config.ANSWER_WINDOW)
    music.unduck()
    answer = voice.screen(heard, "answer") if heard else ""
    _listen["question"] = None
    if answer:
        _send(answer, fresh=False)
    else:
        _post("/api/client/answer", {"source": "pc"})
        state.push_state("idle")


def run(greeting):
    """Start the PC side and loop forever (Ctrl+C or closing the window ends it)."""
    threading.Thread(target=_events, daemon=True, name="client-events").start()
    threading.Thread(target=_presence, daemon=True, name="client-presence").start()
    if _online.wait(8):
        speak(greeting())
    else:
        speak(_UNREACHABLE + " I'll keep trying.")
    print(f"\n  Say 'Jarvis' — commands go to the brain at {config.CLIENT_OF}\n")
    while not state.shutdown.is_set():
        audio.enable_wake()
        while not (audio.activated.wait(timeout=0.1) or _wake_listen.is_set() or state.shutdown.is_set()):
            pass
        if state.shutdown.is_set():
            break
        if audio.activated.is_set():
            _wake_listen.clear()
            _listen["question"] = None            # a new command, not an answer
            _activation()
        elif _wake_listen.is_set():
            _wake_listen.clear()
            if _listen["question"] is not None:
                _answer()
