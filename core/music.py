"""
Music playback: yt-dlp (in-process) resolves YouTube audio, mpv plays it.

YouTube rejects open-ended stream requests (HTTP 403), which is all mpv/ffmpeg
sends, so the audio is downloaded here in bounded byte ranges (like yt-dlp does)
and piped into mpv's stdin.

Autoplay follows YouTube's own "Mix" radio for the first song, and the next
track's stream URL is resolved while the current one plays, so transitions are
near-instant. Volume is controlled over mpv's named-pipe IPC (or pycaw when
available) so music can duck under Jarvis's voice.
"""

import collections
import ctypes
import json
import re
import subprocess
import threading
import time

import requests
from yt_dlp import YoutubeDL

from . import config
from .tts import speak

try:
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
    _PYCAW_AVAILABLE = True
except ImportError:
    _PYCAW_AVAILABLE = False

_MPV_PIPE   = r"\\.\pipe\jarvis_mpv"
DUCK_LEVEL  = 0.08
_HISTORY_MAX = 5
_CHUNK      = 10 * 1024 * 1024   # bytes per range request (yt-dlp's http_chunk_size)
_QUICK_FAIL = 5.0                # a track that ends this fast without a skip counts as failed

# YouTube now requires solving a JS challenge for full-speed, non-403 stream URLs.
# yt-dlp only looks for deno by default; Node works just as well.
_JS_RUNTIMES = {"deno": {}, "node": {}}

_ydl_lock   = threading.Lock()
_ydl_track  = YoutubeDL({"format": "bestaudio/best", "noplaylist": True,
                         "quiet": True, "no_warnings": True, "js_runtimes": _JS_RUNTIMES})
_ydl_mix    = YoutubeDL({"quiet": True, "no_warnings": True,
                         "extract_flat": "in_playlist", "playlistend": 25})

_lock       = threading.Lock()
_proc: "subprocess.Popen | None" = None
_current: dict = {}            # {"id", "title", "url"} of the playing track
_skipped    = False          # set by skip() so a deliberate early end isn't a failure
_session    = 0                # bumped by every explicit play/stop; old monitors bail out
_paused     = False
_ducked     = False            # True while Jarvis is listening/speaking
_vol        = 1.0
_fade_gen   = 0
_fade_lock  = threading.Lock()
_upcoming   = collections.deque()   # video ids queued by autoplay
_prefetched: dict = {}               # next track, already resolved
_history    = collections.deque(maxlen=_HISTORY_MAX)   # previous tracks, oldest first


# ── Status (read by the dashboard) ────────────────────────────────────────────
def is_playing() -> bool:
    return _proc is not None and _proc.poll() is None


def is_paused() -> bool:
    return _paused


def current_title() -> str:
    return _current.get("title", "") if (is_playing() or _paused) else ""


def history_titles() -> list:
    return [t["title"] for t in _history]


def has_track() -> bool:
    return bool(_current)


# ── Volume ────────────────────────────────────────────────────────────────────
def _mpv_send(cmd_list):
    """Send a JSON command to mpv via its named pipe IPC."""
    try:
        handle = ctypes.windll.kernel32.CreateFileW(
            _MPV_PIPE, 0x40000000, 0, None, 3, 0, None  # GENERIC_WRITE, OPEN_EXISTING
        )
        if handle == -1:
            return
        msg = (json.dumps({"command": cmd_list}) + "\n").encode()
        written = ctypes.c_ulong(0)
        ctypes.windll.kernel32.WriteFile(handle, msg, len(msg), ctypes.byref(written), None)
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def _mpv_session():
    """Return the ISimpleAudioVolume for the running mpv process, or None."""
    if not _PYCAW_AVAILABLE or _proc is None:
        return None
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.pid == _proc.pid:
                return s._ctl.QueryInterface(ISimpleAudioVolume)
    except Exception:
        pass
    return None


def _set_vol(level: float):
    global _vol
    _vol = max(0.0, min(1.0, level))
    session = _mpv_session()
    if session:
        session.SetMasterVolume(_vol, None)
    else:
        _mpv_send(["set_property", "volume", int(_vol * 100)])


def _fade_to(target: float, steps: int = 20, duration: float = 1.0):
    global _fade_gen
    with _fade_lock:
        _fade_gen += 1
        my_gen = _fade_gen
    start = _vol
    for i in range(steps):
        if _fade_gen != my_gen:
            return
        _set_vol(start + (target - start) * (i + 1) / steps)
        time.sleep(duration / steps)


def _cancel_fades():
    global _fade_gen
    with _fade_lock:
        _fade_gen += 1


def duck():
    global _ducked
    _ducked = True
    if is_playing():
        threading.Thread(target=_fade_to, args=(DUCK_LEVEL, 20, 0.5), daemon=True).start()


def unduck():
    global _ducked
    _ducked = False
    if is_playing():
        threading.Thread(target=_fade_to, args=(1.0, 20, 1.5), daemon=True).start()


# ── Resolving tracks ──────────────────────────────────────────────────────────
def _resolve(target: str) -> "dict | None":
    """Search query or YouTube URL → {"id", "title", "url"} with a playable stream URL."""
    url = target if target.startswith("http") else f"ytsearch1:{target}"
    with _ydl_lock:
        info = _ydl_track.extract_info(url, download=False)
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else None
    if not info or not str(info.get("url", "")).startswith("http"):
        return None
    size = info.get("filesize")
    if not size:
        m = re.search(r"[?&]clen=(\d+)", info["url"])
        size = int(m.group(1)) if m else None
    return {"id": info.get("id", ""), "title": info.get("title") or target, "url": info["url"],
            "headers": dict(info.get("http_headers") or {}), "size": size}


def _watch_url(track: dict) -> str:
    return f"https://www.youtube.com/watch?v={track['id']}" if track.get("id") else track["title"]


def _load_mix(video_id: str):
    """Queue YouTube's Mix (radio) for this video as the autoplay list."""
    try:
        with _ydl_lock:
            mix = _ydl_mix.extract_info(
                f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}", download=False)
        ids = [e.get("id") for e in (mix.get("entries") or []) if e and e.get("id")]
        recent = {t["id"] for t in _history} | {video_id}
        _upcoming.extend(i for i in ids if i not in recent)
    except Exception as e:
        print(f"  Could not load autoplay mix: {e}")


def _prefetch_next(session: int):
    """Resolve the next autoplay track in the background so it starts instantly."""
    global _prefetched
    if not _upcoming and _current.get("id"):
        _load_mix(_current["id"])
    while _upcoming and session == _session:
        try:
            track = _resolve(_watch_url({"id": _upcoming.popleft()}))
        except Exception:
            continue
        if track and session == _session:
            _prefetched = track
            return


# ── Playback ──────────────────────────────────────────────────────────────────
def _feed(proc, track: dict):
    """Download the stream in bounded range requests and pipe it into mpv's stdin."""
    headers = track.get("headers") or {}
    size    = track.get("size")
    offset  = 0
    try:
        with requests.Session() as http:
            while size is None or offset < size:
                end = offset + _CHUNK - 1
                if size:
                    end = min(end, size - 1)
                with http.get(track["url"], stream=True, timeout=15,
                              headers={**headers, "Range": f"bytes={offset}-{end}"}) as r:
                    if r.status_code not in (200, 206):
                        print(f"  Stream request failed: HTTP {r.status_code}")
                        return
                    got = 0
                    for block in r.iter_content(64 * 1024):
                        if proc.poll() is not None:
                            return       # mpv was stopped/skipped
                        proc.stdin.write(block)
                        got += len(block)
                if got == 0 or (size is None and got < end - offset + 1):
                    return               # reached the end of an unknown-length stream
                offset += got
    except (OSError, ValueError):
        pass                             # mpv closed the pipe
    except Exception as e:
        print(f"  Stream error: {e}")
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


def _launch(track: dict) -> bool:
    global _proc, _current, _skipped
    with _lock:
        try:
            _proc = subprocess.Popen(
                [config.MPV_EXE, "--no-video", "--volume=100", "--cache=yes",
                 f"--input-ipc-server={_MPV_PIPE}",
                 f"--force-media-title={track['title']}", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            speak("mpv isn't installed. Download it from mpv.io and add it to your PATH.")
            return False
        _current = track
        _skipped = False
    threading.Thread(target=_feed, args=(_proc, track), daemon=True).start()
    print(f"  Now playing: {track['title']}")
    time.sleep(0.3)   # give mpv a moment to open the IPC pipe
    # Match the current duck state — a track that finishes loading after Jarvis
    # stopped talking must not stay stuck at duck volume.
    _set_vol(DUCK_LEVEL if _ducked else 1.0)
    return True


def _monitor(session: int):
    """Start the next autoplay track whenever the current one ends."""
    global _prefetched
    failures = 0
    while session == _session:
        proc = _proc
        if proc is None:
            return
        started = time.monotonic()
        proc.wait()
        if session != _session:
            return   # stopped, or a new explicit play started
        # Guard against burning through the whole mix when every track fails to play
        if time.monotonic() - started < _QUICK_FAIL and not _skipped:
            failures += 1
            print(f"  Track ended after {time.monotonic() - started:.1f}s — playback failed.")
            if failures >= 3:
                speak("Music playback keeps failing, so I've stopped it.")
                stop()
                return
        else:
            failures = 0
        track, _prefetched = _prefetched, {}
        if not track:
            _prefetch_next(session)
            track, _prefetched = _prefetched, {}
        if not track or session != _session:
            return
        if _current:
            _history.append(_current)
        if not _launch(track):
            return
        threading.Thread(target=_prefetch_next, args=(session,), daemon=True).start()


def play(target: str, push_history: bool = True):
    """Play a search query or YouTube URL, then keep going with related tracks."""
    global _session, _paused, _prefetched
    previous = dict(_current)
    stop()
    session = _session
    try:
        track = _resolve(target)
    except Exception as e:
        print(f"  yt-dlp error: {e}")
        speak("Music search failed.")
        return
    if session != _session:
        return   # another play/stop happened while we were searching
    if not track:
        speak("Couldn't find that song.")
        return
    if push_history and previous and previous.get("id") != track["id"]:
        _history.append(previous)
    _upcoming.clear()
    _prefetched = {}
    _paused = False
    if not _launch(track):
        return
    threading.Thread(target=_monitor, args=(session,), daemon=True).start()
    threading.Thread(target=_prefetch_next, args=(session,), daemon=True).start()


def stop():
    global _proc, _session, _paused, _current
    _session += 1
    _paused = False
    _cancel_fades()
    with _lock:
        if _proc and _proc.poll() is None:
            try:
                _proc.terminate()
                _proc.wait(timeout=2)
            except Exception:
                pass
        _proc = None
        _current = {}


def pause():
    global _paused
    if is_playing():
        _paused = True
        _mpv_send(["set_property", "pause", True])


def resume():
    global _paused
    if is_playing():
        _paused = False
        _mpv_send(["set_property", "pause", False])


def toggle_pause():
    resume() if _paused else pause()


def skip():
    """End the current track; the monitor starts the next autoplay track."""
    global _paused, _skipped
    _paused = False
    with _lock:
        if _proc and _proc.poll() is None:
            _skipped = True
            try:
                _proc.terminate()
            except Exception:
                pass


def prev(n: int = 1):
    """Play the nth most recent previous track (1 = last played)."""
    if not _history:
        speak("No previous song in history.")
        return
    if n > len(_history):
        speak(f"Only {len(_history)} song{'s' if len(_history) != 1 else ''} in history.")
        return
    track = _history[-n]
    speak(f"Playing {track['title']}.")
    play(_watch_url(track))


def replay():
    """Restart the current track from the beginning."""
    if not _current:
        speak("No song is currently playing.")
        return
    play(_watch_url(_current), push_history=False)
