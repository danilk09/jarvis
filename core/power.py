"""
Battery saving. On battery, Jarvis closes the microphone (and the speaker stream the
voice keeps open) when nobody is around, so the wake-word detector isn't decoding room noise
for hours and open audio streams don't keep Windows from idling to sleep.

Dormant when on battery AND (the PC is locked, or nobody has touched it or given a command
for config.DORMANT_AFTER_MIN minutes), and Jarvis isn't busy or playing music. Any keyboard or
mouse input, unlocking, plugging in, or clicking the orb reopens the mic. Phone commands,
reminders and Claude Code runs keep working throughout; only the wake word sleeps.
"""

import ctypes
import threading
import time

from . import audio, config, music, notify, state, tts

_POLL = 5   # seconds


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_battery():
    try:
        status = _PowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return status.ACLineStatus == 0     # 1 = plugged in, 255 = unknown (desktops)
    except (AttributeError, OSError):
        pass
    return False


def locked():
    """True while the lock screen (or another secure desktop) has the input: a normal
    process can't open the input desktop then."""
    try:
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        desk = user32.OpenInputDesktop(0, False, 0x0001)   # DESKTOP_READOBJECTS
        if not desk:
            return True
        user32.CloseDesktop(ctypes.c_void_p(desk))
    except (AttributeError, OSError):
        pass
    return False


def _should_sleep():
    if not config.DORMANT_AFTER_MIN or not on_battery():
        return False
    if state.dash_state.get("state") != "idle" or state.listening.is_set() or tts.busy():
        return False                     # mid-conversation
    if music.is_playing() and not music.is_paused():
        return False
    return locked() or notify.quiet_seconds() > config.DORMANT_AFTER_MIN * 60


def _loop():
    while True:
        time.sleep(_POLL)
        try:
            if _should_sleep():
                if not audio.paused():
                    reason = "locked" if locked() else f"idle {config.DORMANT_AFTER_MIN} min"
                    print(f"  On battery and {reason} — sleeping the wake word.")
                    audio.pause()
                    tts.release_output()   # an open audio stream can keep Windows awake
            # keyboard/mouse input only: a phone command while you're out shouldn't reopen it
            elif audio.paused() and (not on_battery() or not locked()
                                     and notify.idle_seconds() < config.DORMANT_AFTER_MIN * 60):
                print("  Welcome back — wake word listening again.")
                audio.resume()
        except Exception as e:
            print(f"  Power check failed: {e}")


def start():
    if config.OS == "Windows":
        threading.Thread(target=_loop, daemon=True, name="power").start()
