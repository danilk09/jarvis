"""
Embedded Flask server: dashboard (React build), its JSON API, and the /phone
tap-to-speak page. Optional HTTPS via a Tailscale certificate.
"""

import json
import logging
import os
import queue
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from . import audio, brain, config, music, state
from .media import guess_file_type

app = Flask(__name__, static_folder=config.DASH_BUILD, static_url_path="")
CORS(app)

phone_commands = queue.Queue()   # transcribed phone commands for the main loop

tls = {"cert": "", "key": ""}


# ── Tailscale ─────────────────────────────────────────────────────────────────
def _tailscale_exe():
    """Return the tailscale CLI path, checking common Windows install locations."""
    for c in ("tailscale",
              r"C:\Program Files\Tailscale\tailscale.exe",
              r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
        try:
            if subprocess.run([c, "version"], capture_output=True, timeout=3).returncode == 0:
                return c
        except Exception:
            continue
    return None


def detect_tailscale():
    """Returns (ip, fqdn) e.g. ('100.x.x.x', 'my-pc.tail1234.ts.net') or (None, None)."""
    exe = _tailscale_exe()
    if not exe:
        return None, None
    try:
        ip = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=4).stdout.strip()
        if not ip.startswith("100."):
            return None, None
        data = json.loads(subprocess.run([exe, "status", "--json"],
                                         capture_output=True, text=True, timeout=4).stdout)
        fqdn = data.get("Self", {}).get("DNSName", "").rstrip(".")
        return ip, fqdn or None
    except Exception:
        return None, None


def _cert_days_left(cert_path):
    try:
        not_after = ssl._ssl._test_decode_cert(cert_path)["notAfter"]
        return (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400
    except Exception:
        return -1


def _renew_cert(fqdn):
    exe = _tailscale_exe()
    if not exe:
        return
    try:
        # Renewal talks to Let's Encrypt and can take ~30-40 s
        subprocess.run([exe, "cert", fqdn], capture_output=True, text=True, timeout=120, cwd=config.ROOT)
    except Exception as e:
        print(f"  Tailscale cert renewal failed: {e}")


def setup_tailscale_https(fqdn):
    """
    Use the existing cert when it's valid; renew in the background when it's close
    to expiry (takes effect next start) so startup isn't blocked. Only blocks when
    there's no usable cert at all.
    """
    cert = os.path.join(config.ROOT, f"{fqdn}.crt")
    key  = os.path.join(config.ROOT, f"{fqdn}.key")
    days = _cert_days_left(cert) if os.path.exists(key) else -1
    if days > 14:
        pass
    elif days > 0:
        threading.Thread(target=_renew_cert, args=(fqdn,), daemon=True).start()
    else:
        print("  Requesting a Tailscale TLS cert (can take ~40 s)...")
        _renew_cert(fqdn)
        days = _cert_days_left(cert) if os.path.exists(key) else -1
    if days > 0:
        tls["cert"], tls["key"] = cert, key
        return True
    return False


# ── Status & music ────────────────────────────────────────────────────────────
@app.route("/status")
def _status():
    with state.dash_lock:
        status = dict(state.dash_state)
    # File contents can be large: only send them when the client's count is out of date
    if request.args.get("files", type=int) == len(status["files"]):
        del status["files"]
    status["music_playing"] = music.is_playing()
    status["music_paused"]  = music.is_paused()
    status["current_song"]  = music.current_title()
    status["song_history"]  = music.history_titles()
    return jsonify(status)


@app.route("/api/speech/<int:speech_id>")
def _speech(speech_id):
    with state.dash_lock:
        env = state.speech_envelopes.get(speech_id)
    return (jsonify(env), 200) if env else (jsonify({"error": "unknown speech id"}), 404)


@app.route("/api/activate", methods=["POST"])
def _activate():
    return jsonify({"ok": audio.trigger_wake()})


@app.route("/api/music/control", methods=["POST"])
def _music_control():
    cmd = (request.get_json(force=True) or {}).get("command", "")
    if cmd == "skip":
        music.skip()
    elif cmd == "prev":
        n = int((request.get_json(force=True) or {}).get("n", 1))
        threading.Thread(target=music.prev, args=(n,), daemon=True).start()
    elif cmd == "replay":
        threading.Thread(target=music.replay, daemon=True).start()
    elif cmd == "pause":
        music.pause()
    elif cmd == "resume":
        music.resume()
    elif cmd == "toggle_pause":
        music.toggle_pause()
    elif cmd == "stop":
        music.stop()
    return jsonify({"ok": True})


# ── Workspaces ────────────────────────────────────────────────────────────────
@app.route("/api/workspaces")
def _get_workspaces():
    try:
        with open(config.WORKSPACES_FILE, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({})


@app.route("/api/workspaces", methods=["POST"])
def _save_workspaces():
    data = request.get_json(force=True)
    with open(config.WORKSPACES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    state.workspaces.clear()
    state.workspaces.update(data)
    brain.init_chat_history(state.workspaces)
    return jsonify({"ok": True})


# ── Input folder ──────────────────────────────────────────────────────────────
def _file_info(path, name):
    st = os.stat(path)
    return {"name": name, "size": st.st_size, "modified": st.st_mtime, "type": guess_file_type(name)}


@app.route("/api/input")
def _input_list():
    os.makedirs(config.JARVIS_INPUT_DIR, exist_ok=True)
    files = [_file_info(os.path.join(config.JARVIS_INPUT_DIR, n), n)
             for n in os.listdir(config.JARVIS_INPUT_DIR)
             if os.path.isfile(os.path.join(config.JARVIS_INPUT_DIR, n))]
    files.sort(key=lambda f: f["modified"], reverse=True)
    return jsonify({"files": files})


@app.route("/api/input/upload", methods=["POST"])
def _input_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    safe_name = re.sub(r"[^\w\-. ]", "_", os.path.basename(f.filename))
    os.makedirs(config.JARVIS_INPUT_DIR, exist_ok=True)
    f.save(os.path.join(config.JARVIS_INPUT_DIR, safe_name))
    return jsonify({"ok": True, "filename": safe_name})


@app.route("/api/input/archive")
def _input_archive():
    sessions = []
    if os.path.isdir(config.INPUT_ARCHIVE_DIR):
        for sess in sorted(os.listdir(config.INPUT_ARCHIVE_DIR), reverse=True):
            sess_path = os.path.join(config.INPUT_ARCHIVE_DIR, sess)
            if os.path.isdir(sess_path):
                files = [_file_info(os.path.join(sess_path, n), n) for n in os.listdir(sess_path)
                         if os.path.isfile(os.path.join(sess_path, n))]
                sessions.append({"session": sess, "files": files})
    return jsonify({"sessions": sessions})


@app.route("/api/input/restore", methods=["POST"])
def _input_restore():
    data = request.get_json(force=True) or {}
    # basename(): both values come from the request and must not escape the archive
    session  = os.path.basename(data.get("session", ""))
    filename = os.path.basename(data.get("filename", ""))
    if not session or not filename:
        return jsonify({"error": "Missing session or filename"}), 400
    src = os.path.join(config.INPUT_ARCHIVE_DIR, session, filename)
    if not os.path.isfile(src):
        return jsonify({"error": "File not found"}), 404
    os.makedirs(config.JARVIS_INPUT_DIR, exist_ok=True)
    dest = os.path.join(config.JARVIS_INPUT_DIR, filename)
    if os.path.exists(dest):
        base, ext = os.path.splitext(filename)
        dest = os.path.join(config.JARVIS_INPUT_DIR, f"{base}_{int(time.time())}{ext}")
    shutil.move(src, dest)
    return jsonify({"ok": True})


# ── Phone mic ─────────────────────────────────────────────────────────────────
@app.route("/phone")
def _phone():
    return send_from_directory(config.STATIC_DIR, "phone.html")


@app.route("/phone-command", methods=["POST"])
def _phone_command():
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "no audio"}), 400
    ct = audio_file.content_type or ""
    suffix = ".wav" if "wav" in ct else ".mp4" if ("mp4" in ct or "m4a" in ct) else ".webm"
    src_fd, src_path = tempfile.mkstemp(suffix=suffix)
    wav_path = src_path.rsplit(".", 1)[0] + "_16k.wav"
    try:
        with os.fdopen(src_fd, "wb") as fh:
            audio_file.save(fh)
        r = subprocess.run(["ffmpeg", "-y", "-i", src_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
                           capture_output=True, timeout=15)
        if r.returncode != 0:
            return jsonify({"error": "audio conversion failed"}), 500
        command = audio.transcribe(wav_path)
        if not command:
            return jsonify({"error": "no speech detected"}), 400
        phone_commands.put(command)
        print(f'  Phone command queued: "{command}"')
        return jsonify({"command": command, "status": "processing"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        for p in (src_path, wav_path):
            try:
                os.remove(p)
            except Exception:
                pass


# ── Dashboard (React build; client-side routes fall back to index.html) ──────
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def _dashboard(path):
    if path and os.path.exists(os.path.join(config.DASH_BUILD, path)):
        return send_from_directory(config.DASH_BUILD, path)
    return send_from_directory(config.DASH_BUILD, "index.html")


def start():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    ssl_ctx = (tls["cert"], tls["key"]) if tls["cert"] else None
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True,
                               use_reloader=False, ssl_context=ssl_ctx),
        daemon=True, name="server",
    ).start()
