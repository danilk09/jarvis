"""
24/7 background mode. Only active with `python jarvis.py --background`
(that's what scripts/install_startup.ps1 registers to run at login).

- stdout/stderr go to logs/jarvis.log (rotated) — pythonw.exe has no console,
  and printing with no console would crash
- console programs Jarvis runs (tailscale, ffmpeg, git, PowerShell…) start hidden instead
  of flashing a window each time
- the file, bookmark and app indexes refresh periodically so they stay current
"""

import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time

from . import config


class _LogStream:
    """File-like object that forwards print() output to a logger, line by line."""

    def __init__(self, logger, level):
        self.logger, self.level, self._buf = logger, level, ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, line.rstrip())
        return len(text)

    def flush(self):
        if self._buf.strip():
            self.logger.log(self.level, self._buf.rstrip())
        self._buf = ""

    def isatty(self):          # libraries check this before drawing progress bars/colours
        return False

    encoding = "utf-8"


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(config.LOG_DIR, "jarvis.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger = logging.getLogger("jarvis")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    sys.stdout = _LogStream(logger, logging.INFO)
    sys.stderr = _LogStream(logger, logging.ERROR)


def hide_child_consoles():
    """pythonw.exe has no console, so every console program started without one gets its own
    window. Default them to CREATE_NO_WINDOW; calls that ask for a console keep theirs."""
    original = subprocess.Popen.__init__

    def init(self, *args, creationflags=0, **kwargs):
        if not creationflags & subprocess.CREATE_NEW_CONSOLE:
            creationflags |= subprocess.CREATE_NO_WINDOW
        original(self, *args, creationflags=creationflags, **kwargs)

    subprocess.Popen.__init__ = init


def full_speed():
    """Task Scheduler starts tasks at "below normal" priority, and Windows 11 throttles windowless
    background processes (EcoQoS: slow cores, low clocks — most of all on battery). Jarvis is
    latency-sensitive (Whisper, the voice), so run at normal priority, unthrottled. Programs it
    starts (mpv, Claude Code) inherit this."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class _Throttling(ctypes.Structure):   # PROCESS_POWER_THROTTLING_STATE
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE   # else the 64-bit pseudo-handle is truncated
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    me = k32.GetCurrentProcess()
    k32.SetPriorityClass(me, 0x20)   # NORMAL_PRIORITY_CLASS
    # ControlMask EXECUTION_SPEED with StateMask 0 = never throttle (ProcessPowerThrottling = 4)
    state = _Throttling(1, 1, 0)
    if not k32.SetProcessInformation(me, 4, ctypes.byref(state), ctypes.sizeof(state)):
        print(f"  Could not turn off power throttling (error {ctypes.get_last_error()})")


def start_index_refresh():
    from . import files

    def loop():
        while True:
            time.sleep(config.INDEX_REFRESH_SECS)
            for build in (files.build_file_index, files.build_bookmark_index, files.build_app_index):
                try:
                    build()
                except Exception as e:
                    print(f"  Index refresh failed ({build.__name__}): {e}")

    threading.Thread(target=loop, daemon=True, name="index-refresh").start()
