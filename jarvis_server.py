#!/usr/bin/env python3
"""
jarvis_server.py — lightweight bridge between JARVIS and the web dashboard.
Run this alongside jarvis.py:  python jarvis_server.py

JARVIS posts state updates and files here.
The dashboard polls /status every 1.2s.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import os, time, json, threading

app = Flask(__name__)
CORS(app)  # allow dashboard (file://) to reach localhost

# ── Shared state (thread-safe via lock) ──────────────────────────────────────
_lock  = threading.Lock()
_state = {
    "state":      "idle",   # idle | activated | thinking | speaking | error
    "transcript": "",
    "files":      [],       # list of {name, content, time, size}
}

def update_state(**kwargs):
    with _lock:
        _state.update(kwargs)

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/status")
def status():
    with _lock:
        return jsonify(dict(_state))

@app.route("/state", methods=["POST"])
def set_state():
    """Called by JARVIS to push state changes."""
    data = request.get_json(force=True)
    with _lock:
        if "state"      in data: _state["state"]      = data["state"]
        if "transcript" in data: _state["transcript"]  = data["transcript"]
    return jsonify({"ok": True})

@app.route("/file", methods=["POST"])
def add_file():
    """Called by JARVIS when it creates a file."""
    data = request.get_json(force=True)
    name    = data.get("name", "untitled.txt")
    content = data.get("content", "")
    now     = time.strftime("%H:%M:%S")
    size    = f"{len(content):,} chars"
    with _lock:
        _state["files"].append({
            "name": name,
            "content": content,
            "time": now,
            "size": size,
        })
    print(f"  [server] File received: {name} ({size})")
    return jsonify({"ok": True})

@app.route("/files", methods=["DELETE"])
def clear_files():
    with _lock:
        _state["files"].clear()
    return jsonify({"ok": True})

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*50)
    print("  JARVIS Dashboard Server")
    print("  Listening on http://localhost:5151")
    print("  Open jarvis_dashboard/index.html in your browser")
    print("="*50)
    app.run(host="127.0.0.1", port=5151, debug=False, threaded=True)
