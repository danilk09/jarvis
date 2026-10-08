"""
Microphone input: wake word, command recording and transcription.

A single 16 kHz input stream feeds everything. Each 20 ms frame goes to:
  - a 1 s ring buffer (pre-roll, so "Jarvis, open Discord" in one breath works)
  - the wake-word thread, while wake listening is enabled
  - the command recorder, while a capture is running
"""

import collections
import json
import queue
import re
import threading
import time

import numpy as np
import sounddevice as sd
import vosk
from faster_whisper import WhisperModel

from . import config

try:
    import webrtcvad as _webrtcvad
    _vad = _webrtcvad.Vad(3)
except ImportError:
    _vad = None
    print("  webrtcvad not installed — falling back to amplitude VAD. Run setup.sh to fix.")

vosk.SetLogLevel(-1)

print("  Loading Whisper model (first run may take a moment)...")
WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
whisper_lock  = threading.Lock()
_WHISPER_PROMPT = ("Jarvis, open Discord, search YouTube for, open workspace, never mind, stop, yes, no, "
                   "cancel, open file, find files, take a screenshot, analyze image, what's on screen, "
                   "play, pause, resume, stop")

FRAMES_PER_SEC = config.SAMPLE_RATE // config.FRAME   # 50

_ring       = collections.deque(maxlen=FRAMES_PER_SEC)   # last 1 s of audio
_wake_q     = queue.Queue()
_capture_q  = queue.Queue()
_wake_on    = threading.Event()
_capturing  = threading.Event()
_wake_reset = threading.Event()

activated  = threading.Event()      # set when the wake word is heard
on_wake: list = []                  # callbacks run the instant the wake word fires (stop TTS, duck music)
_wake_info = {"time": 0.0, "preroll": []}

_stream       = None
_stream_lock  = threading.Lock()
_last_frame   = 0.0                 # monotonic time of the last callback (stall detection)
_DEVICE_POLL  = 2.0                 # seconds between default-mic checks


# ── Stream ────────────────────────────────────────────────────────────────────
def _callback(indata, frames, time_info, status):
    global _last_frame
    _last_frame = time.monotonic()
    data = bytes(indata)
    _ring.append(data)
    if _wake_on.is_set():
        _wake_q.put(data)
    if _capturing.is_set():
        _capture_q.put(data)


def _pick_device():
    """config.INPUT_DEVICE (name fragment) if set and present, else the system default."""
    if config.INPUT_DEVICE:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and config.INPUT_DEVICE.lower() in d["name"].lower():
                return i
        print(f'  Input device "{config.INPUT_DEVICE}" not found — using the system default.')
    return None


def _open_stream():
    """(Re)open the shared mic stream. PortAudio caches the device list, so it is
    re-initialised first — otherwise a newly connected headset is invisible."""
    global _stream, _last_frame
    with _stream_lock:
        if _stream is not None:
            try:
                _stream.abort()
                _stream.close()
            except Exception:
                pass
            _stream = None
        sd._terminate()
        sd._initialize()
        device = _pick_device()
        _stream = sd.RawInputStream(samplerate=config.SAMPLE_RATE, blocksize=config.FRAME,
                                    dtype="int16", channels=1, callback=_callback, device=device)
        _last_frame = time.monotonic()
        _stream.start()
        print(f"  Microphone: {sd.query_devices(_stream.device)['name']}")


def _default_mic_id():
    """ID of Windows' current default recording device, or None if unavailable."""
    try:
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetMicrophone().GetId()
    except Exception:
        return None


def _device_watch():
    """Reopen the stream when the default mic changes (AirPods connected, etc.)
    or when the current device stops delivering audio (unplugged)."""
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    last_id = _default_mic_id()
    while True:
        time.sleep(_DEVICE_POLL)
        mic_id  = _default_mic_id()
        changed = mic_id is not None and mic_id != last_id and not config.INPUT_DEVICE
        stalled = time.monotonic() - _last_frame > 3.0
        if not (changed or stalled):
            continue
        print("  Default microphone changed — switching." if changed
              else "  Microphone stopped responding — reopening.")
        if changed:
            time.sleep(1.0)   # give Windows a moment to finish bringing the headset up
        try:
            _open_stream()
            last_id = _default_mic_id() or mic_id
        except Exception as e:
            print(f"  Could not open microphone: {e}")


def start():
    """Open the shared microphone stream and start the wake-word and device-watch threads."""
    threading.Thread(target=_wake_loop, daemon=True, name="wake").start()
    _open_stream()
    threading.Thread(target=_device_watch, daemon=True, name="mic-watch").start()


def _drain(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


# ── Wake word ─────────────────────────────────────────────────────────────────
def enable_wake():
    if not _wake_on.is_set():
        _drain(_wake_q)        # never act on audio from before listening was re-enabled
        _wake_reset.set()
        _wake_on.set()


def disable_wake():
    _wake_on.clear()


def _wake_loop():
    model   = vosk.Model(config.VOSK_MODEL_DIR)
    grammar = json.dumps([config.WAKE_WORD, "[unk]"]) if config.WAKE_GRAMMAR else None

    def new_recognizer():
        if grammar:
            return vosk.KaldiRecognizer(model, config.SAMPLE_RATE, grammar)
        return vosk.KaldiRecognizer(model, config.SAMPLE_RATE)

    rec = new_recognizer()
    batch = []
    while True:
        data = _wake_q.get()
        if _wake_reset.is_set():
            _wake_reset.clear()
            rec = new_recognizer()
            batch.clear()
        batch.append(data)
        if len(batch) < 5:           # feed Vosk 100 ms at a time
            continue
        chunk = b"".join(batch)
        batch.clear()
        if rec.AcceptWaveform(chunk):
            text = json.loads(rec.Result()).get("text", "")
        else:
            text = json.loads(rec.PartialResult()).get("partial", "")
        if config.WAKE_WORD in text.lower() and _wake_on.is_set():
            rec = new_recognizer()
            _fire_wake()


def _fire_wake():
    print("Wake word detected!")
    # Keep the last 1 s from before detection and start recording immediately, in case the
    # command follows the wake word without a pause.
    _wake_info["preroll"] = list(_ring)
    _wake_info["time"] = time.monotonic()
    _drain(_capture_q)
    _capturing.set()
    for cb in on_wake:
        try:
            cb()
        except Exception as e:
            print(f"  on_wake callback error: {e}")
    activated.set()


def trigger_wake() -> bool:
    """Activate as if the wake word was heard (dashboard orb click). False if busy."""
    if not _wake_on.is_set():
        return False
    print("Activated from dashboard.")
    _wake_info["time"] = 0.0     # no one-breath command to capture — go straight to "Yes sir"
    for cb in on_wake:
        try:
            cb()
        except Exception as e:
            print(f"  on_wake callback error: {e}")
    activated.set()
    return True


# ── Recording ─────────────────────────────────────────────────────────────────
def _is_speech(frame: bytes) -> bool:
    if _vad is not None:
        try:
            return _vad.is_speech(frame, config.SAMPLE_RATE)
        except Exception:
            return True
    return int(np.abs(np.frombuffer(frame, dtype=np.int16)).max()) > 650   # ~0.02 full scale


def _record(max_duration, silence_duration, preroll=(), onset_deadline=None, ignore_first=0):
    """
    Pull frames from the capture queue until speech starts and then stops.
    Returns the speech frames, or None if no speech started (before onset_deadline, if given).
    With a preroll, the whole buffer from the preroll onward is kept once speech is confirmed.
    """
    frames       = list(preroll)
    silence_end  = int(silence_duration * FRAMES_PER_SEC)
    max_new      = int(max_duration * FRAMES_PER_SEC)
    speech_start = None
    consec_speech = consec_silence = new = 0
    deadline = time.monotonic() + max_duration + 1.0

    while new < max_new and time.monotonic() < deadline:
        if onset_deadline and speech_start is None and time.monotonic() > onset_deadline:
            break
        try:
            frame = _capture_q.get(timeout=0.1)
        except queue.Empty:
            continue
        frames.append(frame)
        new += 1
        if new <= ignore_first:
            continue
        if _is_speech(frame):
            consec_speech += 1
            consec_silence = 0
            if speech_start is None and consec_speech >= 4:   # ~80 ms of speech confirms onset
                speech_start = 0 if preroll else max(0, len(frames) - 6)
        else:
            consec_speech = 0
            if speech_start is not None:
                consec_silence += 1
                if consec_silence >= silence_end:
                    break

    _capturing.clear()
    if speech_start is None:
        return None
    return frames[speech_start:len(frames) - consec_silence]


def transcribe(audio) -> str:
    """Transcribe a file path or a list of int16 PCM frames."""
    if isinstance(audio, list):
        audio = np.frombuffer(b"".join(audio), dtype=np.int16).astype(np.float32) / 32768.0
    with whisper_lock:
        segments, _ = WHISPER_MODEL.transcribe(
            audio,
            language="en",
            beam_size=1,               # greedy decoding: noticeably faster on CPU for short commands
            without_timestamps=True,
            vad_filter=True,
            initial_prompt=_WHISPER_PROMPT,
            vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        )
        # segments is a lazy generator — decoding happens here, so keep it inside the lock
        return " ".join(s.text.strip() for s in segments).strip()


# The wake word near the start of a transcript (allowing up to two filler words before it)
_WAKE_PREFIX = re.compile(rf"^\W*(?:\w+\W+){{0,2}}?{config.WAKE_WORD}\b\W*", re.IGNORECASE)


def capture_followup():
    """
    Call right after the wake word. If the user kept talking ("Jarvis, open Discord"),
    record and return that command. Returns "" if they paused (caller should prompt),
    or None if the recording doesn't actually start with the wake word — a false
    trigger from background conversation, which the caller should ignore.
    """
    if not _capturing.is_set() or time.monotonic() - _wake_info["time"] > 1.0:
        _capturing.clear()     # stale activation (we were busy) — prompt normally
        return ""
    frames = _record(max_duration=8, silence_duration=0.8,
                     preroll=_wake_info["preroll"],
                     onset_deadline=_wake_info["time"] + config.FOLLOWUP_WINDOW,
                     ignore_first=8)   # skip ~160 ms: the tail end of "Jarvis" itself
    if not frames:
        return ""
    print("  Processing speech...")
    heard = transcribe(frames)
    if not _WAKE_PREFIX.search(heard[:40]):
        print(f'  Ignoring false wake (no "{config.WAKE_WORD}" in: "{heard[:80]}")')
        return None
    text = _WAKE_PREFIX.sub("", heard).strip()
    if text:
        print(f'  Heard: "{text}"')
    return text


def listen_for_command(max_duration=8, silence_duration=0.8) -> str:
    """Record one command (WebRTC VAD end-pointing) and transcribe it."""
    time.sleep(0.3)  # let TTS echo and room reverb die down before capture starts
    _drain(_capture_q)
    _capturing.set()
    print("  Speak your command...")
    frames = _record(max_duration, silence_duration)
    if not frames:
        print("  No speech detected.")
        return ""
    print("  Processing speech...")
    try:
        text = transcribe(frames)
    except Exception as e:
        print(f"  Whisper error: {e}")
        return ""
    if text:
        print(f'  Heard: "{text}"')
    else:
        print("  Could not understand audio.")
    return text


# ── Number selection (file pickers) ───────────────────────────────────────────
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def listen_for_selection():
    from .tts import speak, suppressed, wait_tts
    speak("Let me know when you're ready to select")
    activated.wait(timeout=60)
    activated.clear()
    suppressed.clear()   # the wake word muted TTS; re-enable it for the prompts below
    speak("Which numbers?")
    wait_tts()
    selection = listen_for_command(max_duration=20, silence_duration=3.0)
    if not selection or "cancel" in selection.lower():
        speak("Cancelled.")
        return []
    nums = [int(n) for n in re.findall(r"\b(\d+)\b", selection) if 1 <= int(n) <= 20]
    for word, num in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", selection.lower()):   # avoid "one" in "someone"
            nums.append(num)
    nums = sorted(set(nums))
    if not nums:
        speak("No valid numbers heard. Cancelling.")
    return nums
