"""
Text-to-speech on one dedicated thread: a Piper voice (config.PIPER_VOICE, natural, offline)
or Windows SAPI driven in-process through COM.

Piper (via sherpa-onnx) renders a medium voice ~18x faster than real time. Each line is
split into phrases; the first plays as soon as it's rendered (~50-150 ms) while the rest
render behind it, into an output stream that stays open (opening one costs 100-300 ms).
SAPI: one SpVoice lives on this thread for the whole session, so speech starts almost
immediately. It is also the fallback whenever the Piper voice is missing or fails.

Headless (config.HEADLESS, the Pi brain) there are no speakers: lines are logged and shown on
the dashboard and phone page, and nothing is played.
"""

import collections
import json
import os
import queue
import re
import threading
import time
from xml.sax.saxutils import escape

import numpy as np

from . import config

if config.WINDOWS:
    import comtypes
    import comtypes.client
if not config.HEADLESS:
    import sounddevice as sd
from .state import end_speech, push_log, push_state, speech_beat, start_speech

# SpeechVoiceSpeakFlags
_ASYNC       = 1
_PURGE       = 2
_IS_XML      = 8    # SAPI XML: used only for lines with a custom pronunciation
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
last_spoken   = [0.0]   # when anything was last queued to be said (checkins: "By the way, ..." or not)

# Callbacks run on the TTS thread when Jarvis starts talking and once it has gone
# quiet (jarvis.py ducks music with these, for every voice line — not just replies)
on_speech_start = []
on_speech_end   = []


# ── Pronunciations ────────────────────────────────────────────────────────────
# Words SAPI says wrong ("Danil" → Duh-NEEL): config.PRONUNCIATIONS plus the ones taught by
# voice ("pronounce X as Y"), saved in jarvis_memory/pronunciations.json. Each maps to SAPI
# American English phonemes; only the audio changes, the dashboard and logs keep the spelling.
PRON_FILE = os.path.join(config.MEMORY_DIR, "pronunciations.json")
_pron = {"mtime": None, "map": {}, "re": None}


def pronunciations():
    """{word: phonemes}: config first, the saved file on top (re-read when it changes)."""
    try:
        mtime = os.path.getmtime(PRON_FILE)
    except OSError:
        mtime = 0
    if mtime != _pron["mtime"]:
        saved = {}
        if mtime:
            try:
                with open(PRON_FILE, encoding="utf-8") as f:
                    saved = json.load(f)
            except (OSError, ValueError) as e:
                print(f"  Could not read {PRON_FILE}: {e}")
        words = {**config.PRONUNCIATIONS, **saved}
        _pron.update(mtime=mtime, map={w.lower(): p for w, p in words.items()},
                     re=re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                                   + r")('s)?\b", re.IGNORECASE) if words else None)
    return dict(_pron["map"])


def save_pronunciation(word, phonemes):
    pronunciations()
    saved = {}
    if os.path.exists(PRON_FILE):
        with open(PRON_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    saved[word] = phonemes
    os.makedirs(os.path.dirname(PRON_FILE), exist_ok=True)
    with open(PRON_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f, indent=1)
    _pron["mtime"] = None


def _sapi_xml(text):
    """The line as SAPI XML with <pron> tags, or None if it has no custom pronunciation."""
    pronunciations()
    if not _pron["re"] or not _pron["re"].search(text):
        return None

    def tag(m):
        phones = _pron["map"][m.group(1).lower()] + (" z" if m.group(2) else "")   # "Danil's"
        return f'<pron sym="{phones}">{m.group(0)}</pron>'
    return _pron["re"].sub(tag, escape(text))


def _say(voice, text, flags):
    """voice.Speak with custom pronunciations; plain text if SAPI rejects the phonemes."""
    xml = _sapi_xml(text)
    if xml:
        try:
            return voice.Speak(xml, (flags & ~_IS_NOT_XML) | _IS_XML)
        except Exception as e:
            print(f"  Pronunciation not accepted by SAPI ({e}) — check PRONUNCIATIONS / pronunciations.json")
    return voice.Speak(text, flags)


# SAPI phonemes → a sounds-like spelling Piper's phonemizer reads ("d ax n iy 1 l" → "duh-neel")
_VOWELS  = {"aa": "ah", "ae": "a", "ah": "uh", "ao": "aw", "aw": "ow", "ax": "uh", "ay": "eye",
            "eh": "e", "er": "er", "ey": "ay", "ih": "i", "iy": "ee", "ow": "oh", "oy": "oy",
            "uh": "oo", "uw": "oo"}
_CONSONANTS = {"dh": "th", "jh": "j", "zh": "zh", "hh": "h"}


def _respelled(phonemes):
    phones = [p for p in phonemes.split() if not p.isdigit()]
    syllables, cur, has_vowel = [], "", False
    for i, p in enumerate(phones):
        if p in _VOWELS:
            cur += _VOWELS[p]
            has_vowel = True
            continue
        if has_vowel and i + 1 < len(phones) and phones[i + 1] in _VOWELS:
            syllables.append(cur)   # a consonant before a vowel starts the next syllable
            cur, has_vowel = "", False
        cur += _CONSONANTS.get(p, p)
    return "-".join(syllables + [cur])


def _piper_text(text):
    """The line with custom pronunciations swapped for sounds-like spellings."""
    pronunciations()
    if not _pron["re"]:
        return text
    return _pron["re"].sub(
        lambda m: _respelled(_pron["map"][m.group(1).lower()]) + ("s" if m.group(2) else ""), text)


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
    _say(meter, text, _IS_NOT_XML)
    pcm = np.frombuffer(bytes(stream.GetData()), dtype=np.int16).astype(np.float32) / 32768.0
    return _reduce(pcm)


def _reduce(pcm):
    """16 kHz audio → per-frame band levels and loudness, 0-99, for the dashboard orb."""
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


# ── Piper ─────────────────────────────────────────────────────────────────────
_piper        = {"tts": None, "lock": threading.Lock()}
_piper_loaded = threading.Event()


def _load_piper():
    try:
        if not config.PIPER_VOICE:
            return
        import sherpa_onnx
        if config.WINDOWS:
            comtypes.CoInitialize()   # _speaker_id() asks Windows (COM) which speakers are the default
        d = os.path.join(config.TTS_DIR, config.PIPER_VOICE)
        if not os.path.isdir(d):
            print(f"  Voice folder {d} not found — using the Windows voice")
            return
        onnx = next(f for f in os.listdir(d) if f.endswith(".onnx"))
        tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=os.path.join(d, onnx),
                                                       tokens=os.path.join(d, "tokens.txt"),
                                                       data_dir=os.path.join(d, "espeak-ng-data")),
            num_threads=4)))
        tts.generate("Ready.", sid=0)   # the first call is slower; do it now, not on the greeting
        _piper["tts"] = tts
        _player(tts.sample_rate)        # open the output stream ahead of time too
        threading.Thread(target=_watch_speakers, daemon=True, name="tts-speakers").start()
    except Exception as e:
        print(f"  Piper voice failed to load ({e})" + (" — using the Windows voice" if config.WINDOWS else ""))
    finally:
        _piper_loaded.set()


_ABBREV = re.compile(r"\b(mr|mrs|ms|dr|st|vs|etc|e\.g|i\.e|approx|no)\.$", re.IGNORECASE)


def _phrases(text):
    """[(phrase, pause after it in seconds)]: whole sentences, also split at dashes. Commas stay
    inside so Piper keeps one natural intonation through the sentence (rendering comma pieces
    separately reset it every time); the pauses between sentences are added here."""
    out = []
    text = re.sub(r"\s*[—–]\s*|\s+-\s+", "— ", text.strip())   # "clear — mostly" → "clear—" + "mostly"
    for part in re.split(r"(?<=[.!?—])\s+", text):
        if out and _ABBREV.search(out[-1][0]):   # "Dr. Smith" is one phrase
            out[-1] = (f"{out[-1][0]} {part}", out[-1][1])
        else:
            out.append((part, 0))
    phrases = []
    for phrase, _ in out:
        if not re.search(r"\w", phrase):
            continue
        end = phrase.rstrip()[-1]
        pause = config.PIPER_DASH_PAUSE if end == "—" else config.PIPER_SENTENCE_PAUSE
        phrases.append((phrase, pause))
    return phrases


def _speaker_id():
    """ID of Windows' default speakers. Asks for the endpoint directly: pycaw's
    GetSpeakers() wraps it in an AudioDevice (~110 ms of CPU reading every property) that
    has no GetId()."""
    if not config.WINDOWS:
        return None
    try:
        from pycaw.utils import CLSID_MMDeviceEnumerator, EDataFlow, ERole, IMMDeviceEnumerator
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                         comtypes.CLSCTX_INPROC_SERVER)
        return enum.GetDefaultAudioEndpoint(EDataFlow.eRender.value, ERole.eMultimedia.value).GetId()
    except Exception:
        return None


class _Player:
    """An output stream kept open between lines; audio is queued and pulled by its callback."""

    def __init__(self, sr):
        self.sr, self.chunks, self.queued, self.lock = sr, collections.deque(), 0, threading.Lock()
        self.device = _speaker_id()
        self.stale  = False   # set by _watch_speakers when Windows' default speakers change
        self.stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32", latency="low",
                                      callback=self._fill)
        self.stream.start()

    def _fill(self, out, frames, time_info, status):
        out.fill(0)
        i = 0
        with self.lock:
            while i < frames and self.chunks:
                c = self.chunks[0]
                n = min(len(c), frames - i)
                out[i:i + n, 0] = c[:n]
                i += n
                if n == len(c):
                    self.chunks.popleft()
                else:
                    self.chunks[0] = c[n:]
            self.queued -= i

    def add(self, pcm):
        """Queue audio; returns when its first sample will be heard (epoch seconds)."""
        with self.lock:
            starts = time.time() + self.queued / self.sr + self.stream.latency
            self.chunks.append(pcm)
            self.queued += len(pcm)
        return starts

    def clear(self):
        with self.lock:
            self.chunks.clear()
            self.queued = 0

    def busy(self):
        return self.queued > 0

    def usable(self):
        """False after audio.py re-initialised PortAudio, or when Windows' speakers changed."""
        try:
            return self.stream.active and not self.stale
        except Exception:
            return False

    def close(self):
        try:
            self.stream.abort()
            self.stream.close()
        except Exception:
            pass


_out = {"player": None}


def _player(sr):
    p = _out["player"]
    if p is None or p.sr != sr or not p.usable():
        if p:
            p.close()
        p = _out["player"] = _Player(sr)
    return p


def release_output():
    """Close the always-open speaker stream (battery saving); the next line reopens it."""
    p, _out["player"] = _out["player"], None
    if p:
        p.close()


def _watch_speakers():
    """Checked here every 2 s rather than per line, so a line never waits on Windows."""
    if not config.WINDOWS:
        return
    comtypes.CoInitialize()
    while True:
        time.sleep(2)
        p = _out["player"]
        if p and not p.stale and _speaker_id() != p.device:
            p.stale = True   # the next line reopens the stream on the new speakers


def _onsets(level):
    """Frames where loudness jumps after a dip: syllable-ish beats for the orb (SAPI has words)."""
    beats, low = [], True
    for i, v in enumerate(level):
        if low and v >= 35:
            beats.append(i)
            low = False
        elif v < 20:
            low = True
    return beats


def _speak_piper(text, update_state, lead=0.0):
    """Say `text` with the Piper voice, after `lead` seconds of silence. False if nothing could
    be rendered (SAPI takes over)."""
    tts = _piper["tts"]
    rendered, cancel = queue.Queue(), threading.Event()

    def render():
        try:
            phrases = _phrases(_piper_text(text))
            for n, (phrase, pause) in enumerate(phrases):
                if cancel.is_set():
                    break
                with _piper["lock"]:
                    audio = tts.generate(phrase.rstrip("—").strip(), sid=0, speed=config.PIPER_SPEED)
                sr = audio.sample_rate
                pcm = np.asarray(audio.samples, dtype=np.float32)
                loud = np.flatnonzero(np.abs(pcm) > 0.01)
                if len(loud):   # Piper's own leading/trailing silence varies; replace it with `pause`
                    pcm = pcm[max(0, loud[0] - int(0.02 * sr)):loud[-1] + int(0.03 * sr)]
                    # No pause after the last phrase: whatever comes next (listening for the
                    # command, the next line) shouldn't wait on silence. The worker puts the
                    # gap before a line that follows straight on instead (`lead`).
                    after = pause / config.PIPER_SPEED if n + 1 < len(phrases) else 0
                    before = lead if n == 0 else 0
                    rendered.put(np.concatenate([np.zeros(int(before * sr), np.float32), pcm,
                                                 np.zeros(int(after * sr), np.float32)]))
        except Exception as e:
            rendered.put(e)
        rendered.put(None)

    threading.Thread(target=render, daemon=True, name="tts-render").start()
    player, beats, played, done = None, [], False, False
    try:
        while True:
            if _interrupt.is_set():
                if player:
                    player.clear()
                end_speech()
                return True
            while beats and beats[0] <= time.time():
                beats.pop(0)
                speech_beat()
            if done:
                if not player or not player.busy():
                    time.sleep(player.stream.latency if player else 0)   # let the last block play out
                    return played
                time.sleep(0.02)
                continue
            try:
                item = rendered.get(timeout=0.02)
            except queue.Empty:
                continue
            if item is None or isinstance(item, Exception):
                if isinstance(item, Exception):
                    print(f"  Piper voice error: {item}")
                done = True
                continue
            player = player or _player(tts.sample_rate)
            starts = player.add(item)
            played = True
            if update_state:
                try:
                    n16 = int(len(item) * _ENV_RATE / tts.sample_rate)
                    env = _reduce(np.interp(np.arange(n16) * tts.sample_rate / _ENV_RATE,
                                            np.arange(len(item)), item).astype(np.float32))
                    if env:
                        start_speech(starts, _ENV_FRAME * 1000 // _ENV_RATE, env)
                        beats += [starts + b * _ENV_FRAME / _ENV_RATE for b in _onsets(env["level"])]
                except Exception as e:
                    print(f"  Speech envelope error: {e}")
    finally:
        cancel.set()


def _headless_worker():
    """No speakers: each line is shown (speaking → idle) and its on_start runs, nothing plays."""
    _ready.set()
    while True:
        text, update_state, on_start = _queue.get()
        try:
            if suppressed.is_set():
                continue
            _run_hooks(on_speech_start)
            if update_state:
                push_state("speaking")
            if on_start:
                try:
                    on_start()
                except Exception as e:
                    print(f"  on_start callback error: {e}")
            _run_hooks(on_speech_end)
        finally:
            if update_state and _queue.empty():
                push_state("idle")
            _queue.task_done()


def _worker():
    comtypes.CoInitialize()   # COM objects must be created and used on this thread
    voice = _make_voice()
    meter = _make_voice()     # silent twin that renders to memory for the orb's envelope
    _ready.set()
    talking, ended = False, 0.0   # ended: when the last Piper line finished playing
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
            if config.PIPER_VOICE and _piper_loaded.wait(10) and _piper["tts"]:
                # A line right after another gets the pause between sentences
                lead = max(0.0, config.PIPER_SENTENCE_PAUSE / config.PIPER_SPEED - (time.time() - ended))
                spoke = _speak_piper(text, update_state, lead)
                ended = time.time()
                if spoke:
                    continue
                print("  Piper voice produced nothing — using the Windows voice")
            _say(voice, text, _ASYNC | _IS_NOT_XML)
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


if config.HEADLESS:
    threading.Thread(target=_headless_worker, daemon=True, name="tts").start()
else:
    threading.Thread(target=_load_piper, daemon=True, name="tts-load").start()
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
    last_spoken[0] = time.time()
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
