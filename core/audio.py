"""
Microphone input: wake word, command recording and transcription.

A single 16 kHz input stream feeds everything. Each 20 ms frame goes to:
  - a 4 s ring buffer (pre-roll, so "Jarvis, open Discord" in one breath works even when
    the wake-word detector is running behind)
  - the wake-word thread, while wake listening is enabled
  - the command recorder, while a capture is running

Headless (config.HEADLESS, the Pi brain) there is no mic: Whisper still transcribes phone
recordings, and a "listen" (answering Jarvis's question, a yes/no) waits for the next message
from the phone instead.
"""

import collections
import json
import queue
import re
import threading
import time

import numpy as np
from faster_whisper import WhisperModel

from . import asr, config

if not config.HEADLESS:
    import sounddevice as sd
    import vosk
    vosk.SetLogLevel(-1)

try:
    import webrtcvad as _webrtcvad
    _vad = _webrtcvad.Vad(3)
except ImportError:
    _vad = None
    print("  webrtcvad not installed — falling back to amplitude VAD. Run setup.sh to fix.")

print("  Loading Whisper model (first run may take a moment)...")
WHISPER_MODEL = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
whisper_lock  = threading.Lock()

FRAMES_PER_SEC = config.SAMPLE_RATE // config.FRAME   # 50

_ring       = collections.deque(maxlen=FRAMES_PER_SEC * 4)   # last 4 s of audio (pre-roll)
_levels     = collections.deque(maxlen=FRAMES_PER_SEC * 3)   # RMS of the last 3 s, for the noise floor
_noise      = {"floor": 0.0}                                 # frozen when a recording starts
_wake_q     = queue.Queue()
_capture_q  = queue.Queue()
_wake_on    = threading.Event()
_capturing  = threading.Event()
_wake_reset = threading.Event()

activated  = threading.Event()      # set when the wake word is heard
on_wake: list = []                  # callbacks run the instant the wake word fires (stop TTS, duck music)
_wake_info = {"time": 0.0, "preroll": [], "source": "voice", "lag": 0}
# The last recording: {"frames", "text", "raw", "time"} — voice ID and the voice-data log use it
last_capture: dict = {"frames": [], "text": "", "raw": "", "time": 0.0}
mic = {"name": ""}                  # current input device

_stream       = None
_stream_lock  = threading.Lock()
_paused       = threading.Event()   # mic closed to save battery (power.py)
_last_frame   = 0.0                 # monotonic time of the last callback (stall detection)
_DEVICE_POLL  = 2.0                 # seconds between default-mic checks


# ── Stream ────────────────────────────────────────────────────────────────────
def _callback(indata, frames, time_info, status):
    global _last_frame
    _last_frame = time.monotonic()
    data = bytes(indata)
    _ring.append(data)
    _levels.append(_level(data))
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
        mic["name"] = sd.query_devices(_stream.device)["name"]
        print(f"  Microphone: {mic['name']}")


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
        if _paused.is_set():
            continue
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
    if config.HEADLESS:
        return
    threading.Thread(target=_wake_loop, daemon=True, name="wake").start()
    _open_stream()
    threading.Thread(target=_device_watch, daemon=True, name="mic-watch").start()


def pause():
    """Close the mic (battery saving): no audio is captured or decoded until resume()."""
    global _stream
    if _paused.is_set():
        return
    _paused.set()
    with _stream_lock:
        if _stream is not None:
            try:
                _stream.abort()
                _stream.close()
            except Exception:
                pass
            _stream = None
    _ring.clear()
    print("  Microphone closed to save battery.")


def resume():
    """Reopen the mic after pause()."""
    if not _paused.is_set() or config.HEADLESS:
        return
    try:
        _open_stream()
        _paused.clear()
    except Exception as e:
        print(f"  Could not reopen microphone: {e}")


def paused():
    return _paused.is_set()


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


_MAX_UTTERANCE = 4.0       # seconds: full-vocabulary mode never ends an "utterance" in constant noise
_MAX_BACKLOG   = FRAMES_PER_SEC     # 1 s of queued audio means detection has fallen behind


class _QuietGate:
    """Spares Vosk the silence. Each 100 ms chunk is compared with the room (the 20th
    percentile of the last 10 s of chunk levels): anything clearly above it opens the gate
    and is fed to Vosk together with the 0.4 s before it, so the start of "Jarvis" is never
    lost; the gate stays open until the room has been quiet for 1 s (Vosk hears the silence
    that ends the utterance). In a quiet room that skips most of the decoding; in a loud one
    everything is still decoded."""

    RATIO, HOLD, PREROLL = 1.5, 10, 4     # ~+3.5 dB over the room; chunks of 100 ms

    def __init__(self):
        self.levels = collections.deque(maxlen=100)
        self.quiet  = collections.deque(maxlen=self.PREROLL)
        self.hold   = 0

    def feed(self, chunk):
        """The chunks to decode now ([] while the room is quiet)."""
        level = float(np.sqrt(np.mean(np.frombuffer(chunk, dtype=np.int16).astype(np.float32) ** 2)))
        floor = float(np.percentile(self.levels, 20)) if len(self.levels) >= 20 else 0.0
        self.levels.append(level)
        if level > floor * self.RATIO + 2:
            out = list(self.quiet) + [chunk] if not self.hold else [chunk]
            self.quiet.clear()
            self.hold = self.HOLD
            return out
        if self.hold:
            self.hold -= 1
            return [chunk]
        self.quiet.append(chunk)
        return []


def _wake_loop():
    model   = vosk.Model(config.VOSK_MODEL_DIR)
    words   = [config.WAKE_WORD] + [w for w in getattr(config, "WAKE_DECOYS", []) if w != config.WAKE_WORD]
    grammar = json.dumps(words + ["[unk]"]) if config.WAKE_GRAMMAR else None

    def new_recognizer():
        if grammar:
            return vosk.KaldiRecognizer(model, config.SAMPLE_RATE, grammar)
        return vosk.KaldiRecognizer(model, config.SAMPLE_RATE)

    rec = new_recognizer()
    gate = _QuietGate()
    batch, utt_frames = [], 0
    while True:
        data = _wake_q.get()
        if _wake_reset.is_set():
            _wake_reset.clear()
            rec, utt_frames = new_recognizer(), 0
            batch.clear()
        if _wake_q.qsize() > _MAX_BACKLOG:
            # Fallen behind real time: skip to now rather than drift ever further behind
            # (the pre-roll would no longer contain the wake word anyway)
            _drain(_wake_q)
            rec, utt_frames = new_recognizer(), 0
            batch.clear()
            print("  Wake-word detector fell behind; skipped ahead.")
            continue
        batch.append(data)
        if len(batch) < 5:           # feed Vosk 100 ms at a time
            continue
        chunk = b"".join(batch)
        batch.clear()
        for piece in gate.feed(chunk):
            utt_frames += 5
            if rec.AcceptWaveform(piece):
                text = json.loads(rec.Result()).get("text", "")
                utt_frames = 0
            else:
                text = json.loads(rec.PartialResult()).get("partial", "")
                # (grammar mode is cheap; resetting it mid-word would split "Jarvis" in two)
                if not grammar and utt_frames > _MAX_UTTERANCE * FRAMES_PER_SEC:
                    text += " " + json.loads(rec.FinalResult()).get("text", "")
                    rec, utt_frames = new_recognizer(), 0
            if config.WAKE_WORD in text.lower().split() and _wake_on.is_set():
                rec, utt_frames = new_recognizer(), 0
                _fire_wake()
                break


def _fire_wake():
    # The detector runs behind real time (audio still queued for it, plus Vosk reporting a
    # word a moment after it ends), so "Jarvis" sits further back than the newest audio.
    # Keep 2.5 s before the point the detector had reached, so the word is never cut off —
    # otherwise Whisper doesn't hear "Jarvis" and the command is thrown away as a false wake.
    lag = _wake_q.qsize() + 5
    print("Wake word detected!" + (f" (detector {lag * 1000 // FRAMES_PER_SEC} ms behind)" if lag > 15 else ""))
    _wake_info["preroll"] = list(_ring)[-(int(FRAMES_PER_SEC * 2.5) + lag):]
    _wake_info["lag"] = lag
    _wake_info["time"] = time.monotonic()
    _wake_info["source"] = "voice"
    _measure_noise()
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
    if not _wake_on.is_set() or config.HEADLESS:     # no mic to listen with
        return False
    resume()                     # the mic may be closed to save battery
    print("Activated from dashboard.")
    _wake_info["time"] = 0.0     # no one-breath command to capture — go straight to "Yes sir"
    _wake_info["source"] = "dashboard"
    for cb in on_wake:
        try:
            cb()
        except Exception as e:
            print(f"  on_wake callback error: {e}")
    activated.set()
    return True


# ── Recording ─────────────────────────────────────────────────────────────────
def _measure_noise():
    """Background level right now: the median of the last 3 s (a short command barely moves it)."""
    levels = list(_levels)
    _noise["floor"] = float(np.median(levels)) if levels else 0.0
    _loud.clear()


_GATE_WINDOW = FRAMES_PER_SEC * 2 // 5      # judge loudness over the last 0.4 s...
_GATE_MIN    = _GATE_WINDOW // 5            # ...speech if ≥20% of it is clearly above the room
_loud        = collections.deque(maxlen=_GATE_WINDOW)


def _level(frame: bytes) -> float:
    return float(np.sqrt(np.mean(np.frombuffer(frame, dtype=np.int16).astype(np.float32) ** 2)))


def _is_speech(frame: bytes) -> bool:
    """
    Speech = the VAD says so AND enough of the last 0.4 s is clearly louder than the room's
    background. In a loud place the VAD hears the background as speech too, so without the
    level check a recording never ends until it times out. A single frame is too jumpy to
    judge (crowd noise has loud moments too), hence the short window.
    """
    level = _level(frame)
    _loud.append(level > max(_noise["floor"] * config.SPEECH_OVER_NOISE, 120.0))
    if sum(_loud) < min(_GATE_MIN, len(_loud)):
        return False
    if _vad is not None:
        try:
            return _vad.is_speech(frame, config.SAMPLE_RATE)
        except Exception:
            return True
    return True


def _record(max_duration, silence_duration, preroll=(), onset_deadline=None, ignore_first=0):
    """
    Pull frames from the capture queue until speech starts and then stops.
    Returns the speech frames, or None if no speech started (before onset_deadline, if given).
    With a preroll, the whole buffer from the preroll onward is kept once speech is confirmed.

    Once you've started talking, your own loudness is the yardstick: a frame much quieter than
    your voice (config.SPEECH_END_DROP) is background, however speech-like it sounds. You're
    close to the mic and the room isn't, so this ends a command on time even next to a TV or
    other people talking, where the room-level gate alone keeps hearing "speech". The end is
    `silence_duration` of mostly quiet frames (a stray noise burst no longer restarts the count).
    """
    frames       = list(preroll)
    silence_end  = int(silence_duration * FRAMES_PER_SEC)
    max_new      = int(max_duration * FRAMES_PER_SEC)
    speech_start = None
    consec_speech = new = 0
    voiced = []                                   # loudness of your speech frames so far
    recent = collections.deque(maxlen=silence_end)   # speech? for the last silence_duration
    last_voiced = 0                               # len(frames) after the last speech frame
    deadline = time.monotonic() + max_duration + 1.0
    _noise["peak"] = 0.0

    while new < max_new and time.monotonic() < deadline:
        if onset_deadline and speech_start is None and time.monotonic() > onset_deadline:
            break
        try:
            frame = _capture_q.get(timeout=0.1)
        except queue.Empty:
            continue
        frames.append(frame)
        new += 1
        _noise["peak"] = max(_noise["peak"], _level(frame))
        if new <= ignore_first:
            continue
        speech = _is_speech(frame)
        if speech and speech_start is not None and len(voiced) >= 10:
            speech = _level(frame) >= np.percentile(voiced, 80) * config.SPEECH_END_DROP
        if speech:
            consec_speech += 1
            voiced.append(_level(frame))
            last_voiced = len(frames)
            if speech_start is None and consec_speech >= 4:   # ~80 ms of speech confirms onset
                speech_start = 0 if preroll else max(0, len(frames) - 6)
        else:
            consec_speech = 0
        if speech_start is not None:
            recent.append(speech)
            if len(recent) == silence_end and sum(recent) <= silence_end // 10:
                break

    _capturing.clear()
    if speech_start is None:
        return None
    return frames[speech_start:max(last_voiced, speech_start + 1)]


def transcribe_full(audio):
    """(text, raw text) of a file path or a list of int16 PCM frames. The text has your
    learned fixes applied ("this cord" → "Discord"), see core/speech_data.py."""
    if isinstance(audio, list):
        audio = np.frombuffer(b"".join(audio), dtype=np.int16).astype(np.float32) / 32768.0
    with whisper_lock:
        return asr.transcribe(WHISPER_MODEL, audio)


def transcribe(audio) -> str:
    """Transcribe a file path or a list of int16 PCM frames."""
    return transcribe_full(audio)[0]


# The wake word anywhere in the transcript (Whisper sometimes spells it differently). The
# pre-roll can hold background chatter from before you said it, so anything before is dropped.
_WAKE_VARIANTS = {"jarvis": r"j[ae]rv[ie]s|javis|jarvas|jarvi's|jervis|charvis"}
_WAKE_ANYWHERE = re.compile(rf"\b(?:{_WAKE_VARIANTS.get(config.WAKE_WORD, re.escape(config.WAKE_WORD))})\b\W*",
                            re.IGNORECASE)


def wake_source():
    """How the last activation happened: "voice" (the wake word) or "dashboard" (orb click)."""
    return _wake_info["source"]


def wake_audio(seconds=1.2):
    """The `seconds` before the detector heard the wake word — "Jarvis" itself, for voice ID."""
    pre, lag = _wake_info["preroll"], _wake_info.get("lag", 0)
    end = len(pre) - lag
    return pre[max(0, end - int(seconds * FRAMES_PER_SEC)):max(0, end)]


def _fuzzy_wake(heard):
    """Whisper sometimes spells the wake word oddly ("Travis", "Darvis"). The detector already
    heard it, so a close match in the first two words counts. Returns the end of that word, or None."""
    import difflib
    for m in list(re.finditer(r"[A-Za-z']+", heard))[:2]:
        if difflib.SequenceMatcher(None, m.group(0).lower(), config.WAKE_WORD).ratio() >= 0.65:
            rest = re.match(r"\W*", heard[m.end():])
            return m.end() + (rest.end() if rest else 0)
    return None


def _log_false_wake(heard):
    """Keep the last few false wakes (jarvis_memory/voice/false_wakes.jsonl) to tune against."""
    try:
        import os
        path = os.path.join(config.MEMORY_DIR, "voice", "false_wakes.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "heard": heard,
                                "lag_ms": _wake_info.get("lag", 0) * 1000 // FRAMES_PER_SEC}) + "\n")
    except OSError:
        pass


def _keep(frames, text, raw, voice_from=0):
    """voice_from: where the speaker's own audio starts (the pre-roll can hold someone else)."""
    last_capture.update(frames=frames or [], text=text, raw=raw, time=time.time(), voice_from=voice_from)


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
    t0 = time.monotonic()
    heard, raw = transcribe_full(frames)
    took = time.monotonic() - t0
    voice_from = max(0, len(_wake_info["preroll"]) - _wake_info.get("lag", 0) - int(1.2 * FRAMES_PER_SEC))
    _keep(frames, heard, raw, voice_from=voice_from)
    match = _WAKE_ANYWHERE.search(heard)
    end = match.end() if match else _fuzzy_wake(heard)
    if end is None:
        print(f'  Ignoring false wake (no "{config.WAKE_WORD}" in: "{heard[:80]}")')
        _log_false_wake(heard)
        return None
    if not match:
        print(f'  Wake word heard as "{heard[:end].strip(" ,.")}" — accepting it')
    text = heard[end:].strip()
    if text:
        print(f'  Heard: "{text}" ({took:.1f} s)')
    return text


last_listen = [0.0]   # when the last listen started (so a question it answered isn't asked again)


# Per thread: listen_redirect.fn() answers listen_for_command instead of the mic (client.py,
# when an action runs here for a command that came from the phone)
listen_redirect = threading.local()


def listen_for_command(max_duration=8, silence_duration=0.8, onset_timeout=None) -> str:
    """Record one command (WebRTC VAD end-pointing) and transcribe it. With onset_timeout,
    give up if no speech starts within that many seconds.

    Every listen — after "Yes sir", answering a question, a yes/no confirmation — shows
    LISTENING on the dashboard and pauses the wake word: otherwise saying "Jarvis" (or
    clicking the orb) while Jarvis waits for an answer queues a second activation that only
    fires once this listen times out."""
    from . import state
    hook = getattr(listen_redirect, "fn", None)
    if hook:                     # the PC client running an action for a phone command: ask there
        return hook()
    wake_was_on = _wake_on.is_set()
    disable_wake()
    state.listening.set()
    state.push_state("activated")
    try:
        if config.HEADLESS:
            return _next_message()
        return _listen(max_duration, silence_duration, onset_timeout)
    finally:
        state.listening.clear()
        state.push_state("thinking")     # whoever asked now works with the answer
        if wake_was_on:
            enable_wake()


def _next_message():
    """Headless: the next text sent from the phone, or "" after HEADLESS_ANSWER_WAIT. A photo
    isn't an answer: it goes back in the queue for the main loop."""
    from . import tts, turns
    last_listen[0] = time.time()
    _keep([], "", "")
    print(f"  Waiting for an answer from the {turns.current['source'] or 'phone'}...")
    q = tts.last_question
    turns.listening(q["text"] if time.time() - q["time"] < 120 else "", config.HEADLESS_ANSWER_WAIT)
    try:
        message = turns.queue.get(timeout=config.HEADLESS_ANSWER_WAIT)
    except queue.Empty:
        print("  No answer.")
        return ""
    finally:
        turns.listen_done()
    if message.get("image") or message.get("fresh"):   # a photo, or a new command: not an answer
        turns.queue.put(message)
        return ""
    message = message["text"]
    _keep([], message, message)
    print(f'  Answer: "{message}"')
    return message


def _listen(max_duration, silence_duration, onset_timeout):
    last_listen[0] = time.time()
    time.sleep(0.3)  # let TTS echo and room reverb die down before capture starts
    _measure_noise()
    _drain(_capture_q)
    _capturing.set()
    print("  Speak your command...")
    frames = _record(max_duration, silence_duration,
                     onset_deadline=time.monotonic() + onset_timeout if onset_timeout else None)
    if not frames:
        _keep([], "", "")
        # (room = the noise floor speech must beat; loudest = what the mic picked up meanwhile)
        print(f"  No speech detected (room {_noise['floor']:.0f}, loudest {_noise.get('peak', 0):.0f}, "
              f"mic: {mic.get('name', '?')[:30]}).")
        return ""
    print("  Processing speech...")
    t0 = time.monotonic()
    try:
        text, raw = transcribe_full(frames)
    except Exception as e:
        _keep([], "", "")
        print(f"  Whisper error: {e}")
        return ""
    _keep(frames, text, raw)
    if text:
        print(f'  Heard: "{text}" ({time.monotonic() - t0:.1f} s)')
    else:
        print("  Could not understand audio.")
    return text


# ── Number selection (file pickers) ───────────────────────────────────────────
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def listen_for_selection():
    from .tts import speak, suppressed, wait_tts
    if not getattr(listen_redirect, "fn", None):    # (answered remotely: no wake word needed)
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
