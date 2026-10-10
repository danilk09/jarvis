"""
Text-to-speech via Windows SAPI, driven in-process through COM.

The old approach spawned a PowerShell process per sentence (~0.7 s of startup
before any audio). Here one SpVoice object lives on a dedicated thread for the
whole session, so speech starts almost immediately and can be cut off instantly.
"""

import queue
import threading
import time

import comtypes
import comtypes.client
import numpy as np

from . import config
from .state import end_speech, push_log, push_state, speech_beat, start_speech

# SpeechVoiceSpeakFlags
_ASYNC       = 1
_PURGE       = 2
_IS_NOT_XML  = 16   # treat "<" etc. as plain text
_SAFT16K     = 18   # SpeechAudioFormatType: 16 kHz 16-bit mono

# Spectral envelope for the dashboard orb: 32 ms frames, log-spaced bands over the voice range
_ENV_RATE   = 16000
_ENV_FRAME  = 512
_ENV_BANDS  = 16
_bins       = np.fft.rfftfreq(_ENV_FRAME, 1 / _ENV_RATE)
_edges      = np.geomspace(100, 5000, _ENV_BANDS + 1)
_band_bins  = [np.where((_bins >= lo) & (_bins < hi))[0] for lo, hi in zip(_edges, _edges[1:])]
_band_bins  = [b if len(b) else np.array([np.searchsorted(_bins, lo)]) for b, lo in zip(_band_bins, _edges)]
_window     = np.hanning(_ENV_FRAME).astype(np.float32)

_queue      = queue.Queue()
_ready      = threading.Event()
_interrupt  = threading.Event()   # asks the worker to cut off the current utterance
suppressed  = threading.Event()   # set when the user interrupts; cleared each activation
_LULL       = 0.6                 # seconds of silence before speech counts as finished

# The last question Jarvis asked (a spoken line ending in "?"): jarvis.py listens for the
# answer without the wake word unless something already listened after it
last_question = {"text": "", "time": 0.0}

# Callbacks run on the TTS thread when Jarvis starts talking and once it has gone
# quiet (jarvis.py ducks music with these, for every voice line — not just replies)
on_speech_start = []
on_speech_end   = []


def _run_hooks(hooks):
    for fn in hooks:
        try:
            fn()
        except Exception as e:
            print(f"  Speech hook error: {e}")


def _make_voice():
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    if config.VOICE_NAME:
        tokens = voice.GetVoices()
        for i in range(tokens.Count):
            token = tokens.Item(i)
            if config.VOICE_NAME.lower() in token.GetDescription().lower():
                voice.Voice = token
                break
    voice.Rate = config.SPEECH_RATE
    return voice


def _envelope(meter, text):
    """
    Render `text` silently with a second, identically configured voice (SAPI synthesises
    ~100x faster than real time) and reduce it to per-frame band levels, 0-99.
    The dashboard replays this in sync with the audible speech, equaliser-style.
    """
    stream = comtypes.client.CreateObject("SAPI.SpMemoryStream")
    stream.Format.Type = _SAFT16K
    meter.AudioOutputStream = stream
    meter.Speak(text, _IS_NOT_XML)
    pcm = np.frombuffer(bytes(stream.GetData()), dtype=np.int16).astype(np.float32) / 32768.0
    n = len(pcm) // _ENV_FRAME
    if n == 0:
        return None
    frames = pcm[:n * _ENV_FRAME].reshape(n, _ENV_FRAME)
    spec   = np.abs(np.fft.rfft(frames * _window, axis=1))
    bands  = np.stack([spec[:, b].mean(axis=1) for b in _band_bins], axis=1)
    db     = 20 * np.log10(bands + 1e-6)
    # Per-band peaks compensate for speech's spectral tilt (highs are far quieter), but
    # never more than 30 dB below the loudest band, so near-silent bands stay dark
    peak   = np.maximum(np.percentile(db, 98, axis=0), np.percentile(db, 99) - 30)
    bands  = np.clip((db - (peak - 30)) / 30, 0, 1)            # 30 dB window below each peak
    rms    = np.sqrt((frames ** 2).mean(axis=1))
    level  = np.clip(rms / (np.percentile(rms, 98) + 1e-6), 0, 1)
    return {"bands": (bands * 99).astype(int).tolist(), "level": (level * 99).astype(int).tolist()}


def _worker():
    comtypes.CoInitialize()   # COM objects must be created and used on this thread
    voice = _make_voice()
    meter = _make_voice()     # silent twin that renders to memory for the orb's envelope
    _ready.set()
    talking = False
    while True:
        try:
            # While talking, a short lull ends the speech (not every gap between sentences)
            text, update_state, on_start = _queue.get(timeout=_LULL if talking else None)
        except queue.Empty:
            talking = False
            _run_hooks(on_speech_end)
            continue
        try:
            if suppressed.is_set():   # interrupted between queueing and playback
                continue
            _interrupt.clear()
            if not talking:
                talking = True
                _run_hooks(on_speech_start)
            if update_state:
                push_state("speaking")
            if on_start:
                try:
                    on_start()
                except Exception as e:
                    print(f"  on_start callback error: {e}")
            voice.Speak(text, _ASYNC | _IS_NOT_XML)
            started = time.time()
            if update_state:
                try:
                    env = _envelope(meter, text)
                    if env:
                        start_speech(started, _ENV_FRAME * 1000 // _ENV_RATE, env)
                except Exception as e:
                    print(f"  Speech envelope error: {e}")
            last_word = -1
            while not voice.WaitUntilDone(30):
                if _interrupt.is_set():
                    voice.Speak("", _ASYNC | _PURGE)
                    end_speech()
                    break
                if update_state:
                    # Real word boundaries drive the dashboard's speech animation
                    pos = voice.Status.InputWordPosition
                    if pos != last_word:
                        last_word = pos
                        speech_beat()
        except Exception as e:
            print(f"  TTS error: {e}")
        finally:
            if update_state and _queue.empty():
                push_state("idle")
            _queue.task_done()


threading.Thread(target=_worker, daemon=True, name="tts").start()
_ready.wait(timeout=10)


def speak(text, update_state=True, on_start=None):
    """Queue text to be spoken. on_start runs the moment this sentence starts playing
    (the Stage uses it to highlight what Jarvis is talking about)."""
    if not text:
        return
    if suppressed.is_set():
        print(f"  [muted] JARVIS: {text}")
        return
    print(f"  JARVIS: {text}")
    if str(text).rstrip().endswith("?"):
        last_question.update(text=str(text).strip(), time=time.time())
    if update_state:
        push_log("jarvis", str(text))
    _queue.put((str(text), update_state, on_start))


def stop_tts():
    """Cut off current speech and drop anything queued."""
    suppressed.set()
    _interrupt.set()
    while True:
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            break


def wait_tts():
    """Block until everything queued has been spoken."""
    _queue.join()


def busy():
    """True while anything is queued or being spoken."""
    return _queue.unfinished_tasks > 0
