"""
24/7 background mode. Only active with `python jarvis.py --background`
(that's what scripts/install_startup.ps1 registers to run at login).

- stdout/stderr go to logs/jarvis.log (rotated) — pythonw.exe has no console,
  and printing with no console would crash
- the file, bookmark and app indexes refresh periodically so they stay current
"""

import logging
import logging.handlers
import os
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
