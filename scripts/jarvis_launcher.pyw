"""
What the Jarvis shortcut runs (scripts/install_shortcut.ps1 creates it). If Jarvis is
already running, it brings its window up; otherwise it starts Jarvis hidden (background
mode: output in logs/jarvis.log) with the window open. Closing the window shuts Jarvis
down again (config.QUIT_WITH_WINDOW).
"""

import os
import ssl
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 5151   # config.PORT (not imported: loading core/config.py builds the Anthropic client)


def show_window():
    """Ask a running Jarvis to open its window. False if Jarvis isn't running."""
    insecure = ssl.create_default_context()
    insecure.check_hostname = False   # the Tailscale certificate is for the machine's name, not localhost
    insecure.verify_mode = ssl.CERT_NONE
    for scheme in ("https", "http"):
        try:
            req = urllib.request.Request(f"{scheme}://localhost:{PORT}/api/window", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3, context=insecure if scheme == "https" else None)
            return True
        except Exception:
            continue
    return False


if not show_window():
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    subprocess.Popen([pythonw if os.path.exists(pythonw) else sys.executable,
                      os.path.join(ROOT, "jarvis.py"), "--background", "--window"],
                     cwd=ROOT, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
