"""
Voice ID: does this recording sound like you?

A pretrained speaker model (NVIDIA NeMo TitaNet-small, ONNX, run by sherpa-onnx on the CPU in ~80 ms)
turns the speech in a recording into a 512-number voiceprint. Scores are the cosine
similarity to the voiceprints saved when you enrolled ("Jarvis, learn my voice" — see
core/voice.py), averaged over the closest few, so each microphone you enrolled on
counts on its own.

    jarvis_memory/voice/profile.npz     your voiceprints (enrolled + adapted)
    jarvis_memory/voice/settings.json   mode set by voice ("turn on voice lock")
    jarvis_memory/voice/visitors.jsonl  people Jarvis turned away

Recordings that score very high are added to the profile (up to _ADAPT_MAX), so it follows
your voice through a cold or a new room. Only clear matches are added, so it can't drift
toward someone else. This is a convenience filter, not security: a recording of your voice
gets through.
"""

import json
import os
import re
import threading
import time

import numpy as np

from . import config, speech_data

PROFILE_FILE  = os.path.join(speech_data.VOICE_DIR, "profile.npz")
SETTINGS_FILE = os.path.join(speech_data.VOICE_DIR, "settings.json")
VISITORS_FILE = os.path.join(speech_data.VOICE_DIR, "visitors.jsonl")

_ADAPT_MAX    = 40      # adapted voiceprints kept (oldest dropped first)
_TOP_K        = 5       # score = mean similarity to the closest K voiceprints
_SHORT        = 1.0     # seconds of speech below which scores run lower...
_SHORT_MARGIN = 0.08    # ...so both thresholds drop by this much

_lock = threading.RLock()
_extractor = None
_load_error = ""
_emb  = np.zeros((0, 0), dtype=np.float32)
_meta: list = []        # [{"mic", "kind": "enroll"|"adapt", "time"}] parallel to _emb rows

# The current command's result, for handlers that need to know who's talking
current: dict = {"verdict": "none", "score": None}

try:
    import webrtcvad as _webrtcvad
    _vad = _webrtcvad.Vad(2)
except ImportError:
    _vad = None


# ── Model and profile ─────────────────────────────────────────────────────────
def _load():
    """Load the speaker model and profile once. False if voice ID can't run."""
    global _extractor, _load_error, _emb, _meta
    with _lock:
        if _extractor is not None or _load_error:
            return _extractor is not None
        try:
            import sherpa_onnx
            if not os.path.exists(config.VOICE_MODEL):
                raise FileNotFoundError(f"speaker model missing: {config.VOICE_MODEL} (run setup.sh)")
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=config.VOICE_MODEL, num_threads=2))
        except Exception as e:
            _load_error = str(e) if not isinstance(e, ImportError) else "sherpa-onnx not installed (pip install sherpa-onnx)"
            print(f"  Voice ID off: {_load_error}")
            return False
        try:
            data = np.load(PROFILE_FILE, allow_pickle=False)
            _emb = data["emb"].astype(np.float32)
            _meta = [{"mic": str(m), "kind": str(k), "time": float(t)}
                     for m, k, t in zip(data["mic"], data["kind"], data["time"])]
        except FileNotFoundError:
            _emb, _meta = np.zeros((0, _extractor.dim), dtype=np.float32), []
        return True


def available():
    return _load()


def _save():
    with _lock:
        os.makedirs(speech_data.VOICE_DIR, exist_ok=True)
        tmp = PROFILE_FILE + ".tmp.npz"
        np.savez(tmp, emb=_emb, mic=np.array([m["mic"] for m in _meta], dtype=str),
                 kind=np.array([m["kind"] for m in _meta], dtype=str),
                 time=np.array([m["time"] for m in _meta], dtype=np.float64))
        os.replace(tmp, PROFILE_FILE)


def enrolled_mics():
    """{mic name: enrolled voiceprints}."""
    _load()
    out = {}
    for m in _meta:
        if m["kind"] == "enroll":
            out[m["mic"]] = out.get(m["mic"], 0) + 1
    return out


def enrolled():
    return bool(enrolled_mics())


def counts():
    _load()
    return {"enroll": sum(m["kind"] == "enroll" for m in _meta), "adapt": sum(m["kind"] == "adapt" for m in _meta)}


def mode():
    """"off" | "shadow" | "enforce" — a spoken "voice lock on/off" overrides config.VOICE_ID_MODE."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f).get("mode") or config.VOICE_ID_MODE
    except (FileNotFoundError, ValueError):
        return config.VOICE_ID_MODE


def set_mode(value):
    os.makedirs(speech_data.VOICE_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": value}, f)


def enforcing():
    return mode() == "enforce" and enrolled() and available()


# ── Scoring ───────────────────────────────────────────────────────────────────
def _voiced(frames):
    """Only the frames with speech in them: silence and room noise water a voiceprint down."""
    if _vad is None:
        return frames
    keep = []
    for f in frames:
        try:
            if len(f) == config.FRAME * 2 and _vad.is_speech(f, config.SAMPLE_RATE):
                keep.append(f)
        except Exception:
            keep.append(f)
    return keep if len(keep) >= 25 else frames      # under 0.5 s of speech: use it all


def embed(frames):
    """(normalized voiceprint, seconds of speech used) for a list of int16 PCM frames, or (None, 0)."""
    if not frames or not _load():
        return None, 0.0
    voiced = _voiced(frames)
    audio = speech_data.to_float(voiced)
    if len(audio) < config.SAMPLE_RATE * 0.3:
        return None, 0.0
    with _lock:
        stream = _extractor.create_stream()
        stream.accept_waveform(config.SAMPLE_RATE, audio)
        stream.input_finished()
        if not _extractor.is_ready(stream):
            return None, 0.0
        vec = np.array(_extractor.compute(stream), dtype=np.float32)
    return vec / (np.linalg.norm(vec) + 1e-9), len(audio) / config.SAMPLE_RATE


def score(vec, exclude=None):
    """Mean cosine similarity to the closest _TOP_K voiceprints (optionally leaving one row out)."""
    with _lock:
        if vec is None or not len(_emb):
            return None
        sims = _emb @ vec
        if exclude is not None:
            sims = np.delete(sims, exclude)
        if not len(sims):
            return None
        top = np.sort(sims)[-_TOP_K:]
        return float(top.mean())


def check(frames, mic=None):
    """
    Who said this? {"verdict", "score", "seconds", "vec"} where verdict is
      "owner" | "unsure" | "other" — the score against your profile
      "new_mic"  — not a clear match, but you never enrolled on this microphone
      "none"     — voice ID is off, not enrolled, or there wasn't enough speech
    """
    result = {"verdict": "none", "score": None, "seconds": 0.0, "vec": None}
    if mode() == "off" or not enrolled():
        return result
    vec, seconds = embed(frames)
    s = score(vec)
    if s is None:
        return result
    margin = _SHORT_MARGIN if seconds < _SHORT else 0.0
    if s >= config.VOICE_ACCEPT - margin:
        verdict = "owner"
    elif mic is not None and mic not in enrolled_mics():
        verdict = "new_mic"
    elif s < config.VOICE_REJECT - margin:
        verdict = "other"
    else:
        verdict = "unsure"
    result.update(verdict=verdict, score=round(s, 3), seconds=round(seconds, 2), vec=vec)
    return result


def describe(result):
    if result.get("score") is None:
        return "voice ID: -"
    return f'voice ID: {result["verdict"]} ({result["score"]:.2f}, {result["seconds"]:.1f} s)'


# ── Profile changes ───────────────────────────────────────────────────────────
def add_enrollment(vec, mic):
    global _emb
    with _lock:
        _emb = np.vstack([_emb, vec[None, :]]) if len(_emb) else vec[None, :].copy()
        _meta.append({"mic": mic, "kind": "enroll", "time": time.time()})
        _save()


def adapt(result, mic):
    """Add a clear match (VOICE_ADAPT+, 1.2 s+ of speech) to the profile."""
    global _emb
    vec = result.get("vec")
    if vec is None or (result.get("score") or 0) < config.VOICE_ADAPT or result.get("seconds", 0) < 1.2:
        return
    with _lock:
        _emb = np.vstack([_emb, vec[None, :]])
        _meta.append({"mic": mic, "kind": "adapt", "time": time.time()})
        adapted = [i for i, m in enumerate(_meta) if m["kind"] == "adapt"]
        if len(adapted) > _ADAPT_MAX:
            drop = adapted[0]
            _emb = np.delete(_emb, drop, axis=0)
            del _meta[drop]
    threading.Thread(target=_save, daemon=True).start()


def reset():
    global _emb, _meta
    with _lock:
        if _extractor is not None:
            _emb = np.zeros((0, _extractor.dim), dtype=np.float32)
        _meta = []
        try:
            os.remove(PROFILE_FILE)
        except FileNotFoundError:
            pass


def self_scores():
    """Leave-one-out score of every enrolled voiceprint — how consistent your enrollment is."""
    _load()
    return [score(_emb[i], exclude=i) for i, m in enumerate(_meta) if m["kind"] == "enroll"]


# ── People ────────────────────────────────────────────────────────────────────
def owner_name():
    if config.OWNER_NAME:
        return config.OWNER_NAME
    try:
        from . import memory
        for f in memory.facts():
            m = re.search(r"\b(?:name is|called|goes by)\s+([A-Z][\w'-]+)", f["text"])
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def log_visitor(name, said, result):
    os.makedirs(speech_data.VOICE_DIR, exist_ok=True)
    with open(VISITORS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "name": name, "said": said,
                            "score": result.get("score")}, ensure_ascii=False) + "\n")
