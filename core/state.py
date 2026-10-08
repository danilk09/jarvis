"""
State shared between the voice loop, the action handlers and the dashboard server.
"""

import threading
import time

# Dashboard status, polled by the React app via GET /status
dash_lock = threading.Lock()
dash_state: dict = {
    "state":       "idle",   # idle | activated | thinking | speaking | error
    "transcript":  "",
    "files":       [],
    "speech_beat": 0,        # increments once per spoken word
    "speech":      None,     # {"id", "start", "end", "frame_ms", "frames"} of the current utterance
    "log":         [],       # recent conversation: {"role": "user"|"jarvis", "text", "time"}
}
_LOG_MAX = 40

# Spectral envelopes of recent utterances, fetched once per utterance via /api/speech/<id>
speech_envelopes: dict = {}
_speech_id = 0

# Workspace config — mutated in place so every module sees hot-reloads from the dashboard
workspaces: dict = {}

# Set at startup to jarvis_input/archive/session_YYYYMMDD_HHMMSS/
session_archive = ""


def push_state(state, transcript=""):
    with dash_lock:
        dash_state["state"] = state
        if transcript:
            dash_state["transcript"] = transcript
    if transcript:
        push_log("user", transcript)


def push_log(role, text):
    entry = {"role": role, "text": text, "time": time.strftime("%H:%M:%S")}
    with dash_lock:
        dash_state["log"] = (dash_state["log"] + [entry])[-_LOG_MAX:]


def push_file(name, content):
    entry = {"name": name, "content": content,
             "time": time.strftime("%H:%M:%S"), "size": f"{len(content):,} chars"}
    with dash_lock:
        dash_state["files"].append(entry)


def speech_beat():
    with dash_lock:
        dash_state["speech_beat"] += 1


def start_speech(start, frame_ms, envelope):
    """Publish the envelope of an utterance that started playing at `start` (epoch seconds)."""
    global _speech_id
    with dash_lock:
        _speech_id += 1
        speech_envelopes[_speech_id] = envelope
        for old in [k for k in speech_envelopes if k <= _speech_id - 5]:
            del speech_envelopes[old]
        dash_state["speech"] = {"id": _speech_id, "start": int(start * 1000), "end": None,
                                "frame_ms": frame_ms, "frames": len(envelope["level"])}


def end_speech():
    with dash_lock:
        if dash_state["speech"] and dash_state["speech"]["end"] is None:
            dash_state["speech"] = {**dash_state["speech"], "end": int(time.time() * 1000)}
