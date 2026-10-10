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
    "log":         [],       # recent conversation: {"role": "user"|"jarvis", "text", "time", "sample"?}
    "log_rev":     0,        # bumped when an earlier entry changes (a corrected transcript)
}
_LOG_MAX = 40

# Spectral envelopes of recent utterances, fetched once per utterance via /api/speech/<id>
speech_envelopes: dict = {}
_speech_id = 0

# Workspace config — mutated in place so every module sees hot-reloads from the dashboard
workspaces: dict = {}

# Set at startup to jarvis_input/archive/session_YYYYMMDD_HHMMSS/
session_archive = ""

# Dashboard URL (http://localhost:5151, or the Tailscale HTTPS address)
dash_url = ""


def push_state(state, transcript="", sample=""):
    """sample: id of the recording the transcript came from (lets the dashboard correct it)."""
    with dash_lock:
        dash_state["state"] = state
        if transcript:
            dash_state["transcript"] = transcript
    if transcript:
        push_log("user", transcript, sample=sample)


# Called with (role, text) for every logged line (memory.py keeps the conversation on disk)
on_log: list = []


def push_log(role, text, sample=""):
    entry = {"role": role, "text": text, "time": time.strftime("%H:%M:%S")}
    if sample:
        entry["sample"] = sample
    with dash_lock:
        dash_state["log"] = (dash_state["log"] + [entry])[-_LOG_MAX:]
    for fn in on_log:
        try:
            fn(role, text)
        except Exception as e:
            print(f"  Log hook error: {e}")


def mark_log(sample, **fields):
    """Update the log entry of a recording: text=… corrected=True, or confirmed=True."""
    with dash_lock:
        for entry in dash_state["log"]:
            if entry.get("sample") == sample:
                entry.update(fields)
        dash_state["log_rev"] += 1


def push_file(name, content):
    entry = {"name": name, "content": content,
             "time": time.strftime("%H:%M:%S"), "size": f"{len(content):,} chars"}
    with dash_lock:
        dash_state["files"].append(entry)


def update_file(name, content):
    """Keep the dashboard's file list in sync with edits saved from the Stage."""
    with dash_lock:
        for entry in reversed(dash_state["files"]):
            if entry["name"] == name:
                entry.update(content=content, size=f"{len(content):,} chars")
                break


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
