"""
Opens the dashboard: in the Electron desktop app (desktop/) when it is installed,
otherwise in the default browser. The desktop app is what lets Stage panels show
live web pages that Jarvis can read, scroll and highlight.
"""

import os
import subprocess
import threading
import webbrowser

from . import config

_proc = None
_lock = threading.Lock()


def electron_exe():
    exe = os.path.join(config.DESKTOP_DIR, "node_modules", "electron", "dist", "electron.exe")
    return exe if os.path.exists(exe) else None


def open_dashboard(url):
    global _proc
    exe = electron_exe() if config.USE_DESKTOP_APP else None
    if not exe:
        if config.USE_DESKTOP_APP:
            print("  Desktop app not installed (cd desktop && npm install) — using the browser.")
        webbrowser.open(url)
        return
    with _lock:
        if _proc and _proc.poll() is None:
            return     # already open; the app keeps itself to a single window
        env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
        env["JARVIS_URL"] = url
        _proc = subprocess.Popen([exe, config.DESKTOP_DIR], env=env, cwd=config.DESKTOP_DIR,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
