"""
Voice data: every command Jarvis hears (audio + transcript), the corrections you make, the
fixes learned from them and your personal vocabulary. Feeds core/asr.py (vocabulary in the
Whisper prompt, learned fixes), the dashboard's transcript editing, and the scripts in
scripts/voice/ (benchmark, labeler, fine-tune export).

    jarvis_memory/voice/samples/<day>/<id>.wav   16 kHz mono recordings
    jarvis_memory/voice/samples.jsonl            one line per recording; later lines add labels
    jarvis_memory/voice/corrections.json         "heard → meant" pairs and how often each happened
    jarvis_memory/voice/vocabulary.txt           words Whisper should expect, one per line (edit freely)

A label is the true text of a recording: from the enrollment script, "no, I said …", the
dashboard (edit / ✓), or scripts/voice/label.py. Labeled recordings are kept forever (they are
the benchmark and fine-tune data); unlabeled ones are pruned after VOICE_SAMPLE_DAYS.
"""

import difflib
import json
import os
import re
import shutil
import threading
import time
import wave

import numpy as np

from . import config

VOICE_DIR        = os.path.join(config.MEMORY_DIR, "voice")
SAMPLES_DIR      = os.path.join(VOICE_DIR, "samples")
SAMPLES_FILE     = os.path.join(VOICE_DIR, "samples.jsonl")
CORRECTIONS_FILE = os.path.join(VOICE_DIR, "corrections.json")
VOCAB_FILE       = os.path.join(VOICE_DIR, "vocabulary.txt")

_lock = threading.Lock()
_recent: list = []          # [(id, time, heard)] of this run's recordings, newest last

_VOCAB_HEADER = """# Words and names Jarvis should expect to hear — one per line. They go into the
# speech-to-text prompt, so Whisper spells them right. Keep it to names it gets wrong
# (people, apps, projects, places); about 30 at most. Lines starting with # are ignored.
"""


def _ensure():
    os.makedirs(SAMPLES_DIR, exist_ok=True)


# ── Recordings ────────────────────────────────────────────────────────────────
def to_float(audio):
    """int16 PCM frames (list of bytes) or a float array → float32 array in [-1, 1]."""
    if isinstance(audio, (list, tuple)):
        return np.frombuffer(b"".join(audio), dtype=np.int16).astype(np.float32) / 32768.0
    return np.asarray(audio, dtype=np.float32)


def write_wav(path, audio):
    pcm = (np.clip(to_float(audio), -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def read_wav(path):
    with wave.open(path, "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def _append(record):
    _ensure()
    with _lock, open(SAMPLES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_sample(audio, heard, raw=None, source="listen", text=None, label_source=None, **meta):
    """Save one recording and its transcript. Returns its id ("" when saving is off)."""
    if not config.VOICE_SAVE_SAMPLES or audio is None or not len(audio):
        return ""
    now = time.time()
    sid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
    rel = os.path.join(sid[:8], sid + ".wav")
    try:
        _ensure()
        os.makedirs(os.path.join(SAMPLES_DIR, sid[:8]), exist_ok=True)
        write_wav(os.path.join(SAMPLES_DIR, rel), audio)
    except Exception as e:
        print(f"  [voice] could not save recording: {e}")
        return ""
    record = {"id": sid, "time": round(now, 2), "wav": rel.replace("\\", "/"), "heard": heard,
              "raw": raw if raw is not None else heard, "source": source, "model": config.WHISPER_MODEL,
              **{k: v for k, v in meta.items() if v is not None}}
    if text is not None:
        record.update(text=text, label_source=label_source or "script")
    _append(record)
    with _lock:
        _recent.append((sid, now, heard))
        del _recent[:-20]
    return sid


def recent_sample(skip=0, max_age=180):
    """(id, heard) of this run's latest recording, skipping the newest `skip` ones; None if too old."""
    with _lock:
        if len(_recent) <= skip:
            return None
        sid, when, heard = _recent[-1 - skip]
    return (sid, heard) if time.time() - when <= max_age else None


def load_samples():
    """Every recording, labels merged in: {id: record}."""
    out = {}
    try:
        with open(SAMPLES_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("id") in out:
                    out[rec["id"]].update(rec)
                elif "wav" in rec:
                    out[rec["id"]] = rec
    except FileNotFoundError:
        pass
    return out


def labeled():
    """Recordings with a known true text whose audio is still on disk."""
    return [r for r in load_samples().values()
            if r.get("text") and os.path.exists(os.path.join(SAMPLES_DIR, r["wav"]))]


def label(sid, text, how):
    """Record the true text of a recording; learns a fix if it differs from what was heard."""
    text = (text or "").strip()
    rec = load_samples().get(sid)
    if not rec or not text:
        return False
    if how in ("voice", "dashboard", "confirmed") and rec.get("prefix"):
        text = rec["prefix"] + text      # the dashboard shows the command; the audio starts with "Jarvis, "
    _append({"id": sid, "text": text, "label_source": how, "labeled": round(time.time(), 2)})
    if how != "confirmed" and norm(rec.get("heard", "")) != norm(text):
        learn(rec.get("heard", ""), text)
    return True


def prune():
    """Delete unlabeled recordings older than VOICE_SAMPLE_DAYS (labeled ones are kept)."""
    if not os.path.isdir(SAMPLES_DIR):
        return
    keep = {r["wav"] for r in load_samples().values() if r.get("text")}
    keep_days = {w[:8] for w in keep}
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - config.VOICE_SAMPLE_DAYS * 86400))
    removed = 0
    for day in os.listdir(SAMPLES_DIR):
        if not day.isdigit() or day >= cutoff:
            continue
        folder = os.path.join(SAMPLES_DIR, day)
        if day not in keep_days:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
            continue
        for name in os.listdir(folder):
            if f"{day}/{name}" not in keep:
                os.remove(os.path.join(folder, name))
    if removed:
        print(f"  [voice] pruned {removed} day(s) of old unlabeled recordings")


# ── Text helpers ──────────────────────────────────────────────────────────────
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_NUMBER = {w: str(i) for i, w in enumerate(_ONES)} | {w: str(i * 10) for i, w in enumerate(_TENS) if w}


def _digits(words):
    """"twenty five" → "25", "three" → "3": Whisper writes numbers either way."""
    out = []
    for w in words:
        if w in _NUMBER and out and out[-1].endswith("0") and len(out[-1]) == 2 and len(_NUMBER[w]) == 1 and _NUMBER[w] != "0":
            out[-1] = out[-1][0] + _NUMBER[w]
        else:
            out.append(_NUMBER.get(w, w))
    return out


def norm(text):
    """Lowercase words without punctuation, numbers as digits, for comparing transcripts."""
    text = re.sub(r"[^\w\s']", " ", (text or "").lower().replace("-", " ")).replace("'", "")
    return " ".join(_digits(text.split()))


def word_errors(ref, hyp):
    """(word edits, reference words) — the word error rate is their ratio."""
    r, h = norm(ref).split(), norm(hyp).split()
    row = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, row[0] = row[0], i
        for j in range(1, len(h) + 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return row[len(h)], len(r)


# "no, I said open Discord" / "I meant Discord" / "that's not what I said, I said …"
_CORRECTION = re.compile(
    r"^\W*(?:no\W+(?:jarvis\W+)?i (?:said|meant)|i meant|(?:that's|that is) not what i said\W+(?:i said)?|correction)\W+(.+)$",
    re.IGNORECASE)


def parse_correction(command):
    """The corrected text if this command is a correction of the last one, else None."""
    m = _CORRECTION.match(command or "")
    return m.group(1).strip().rstrip(".") if m else None


# ── Learned fixes ─────────────────────────────────────────────────────────────
_STOP = {"a", "an", "the", "to", "of", "and", "in", "on", "it", "is", "i", "you", "for", "at", "my", "me"}
_rules_cache = {"mtime": None, "rules": []}


def _load_corrections():
    try:
        with open(CORRECTIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"pairs": {}}


def learn(heard, meant):
    """Remember which words were misheard as what. A pair seen VOICE_RULE_MIN times becomes a fix."""
    h_words, m_words = heard.split(), meant.split()
    h_norm, m_norm = [norm(w) for w in h_words], [norm(w) for w in m_words]
    data = _load_corrections()
    learned = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, h_norm, m_norm, autojunk=False).get_opcodes():
        if op != "replace" or i2 - i1 > 4 or j2 - j1 > 4:
            continue
        wrong = " ".join(w for w in h_norm[i1:i2] if w)
        right = " ".join(re.sub(r"[^\w'\-]", "", w) for w in m_words[j1:j2]).strip()
        if len(wrong) < 3 or not right or wrong in _STOP or norm(right) == wrong:
            continue
        pair = data["pairs"].setdefault(f"{wrong}|{norm(right)}", {"heard": wrong, "meant": right, "count": 0})
        pair.update(meant=right, count=pair["count"] + 1, last=round(time.time()))
        learned.append(f'"{wrong}" → "{right}" (x{pair["count"]})')
    if learned:
        _ensure()
        with open(CORRECTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        _rules_cache["mtime"] = None     # two writes can share a timestamp; don't trust mtime alone
        print("  [voice] learned: " + ", ".join(learned).replace("→", "->"))


def corrections():
    """Every heard → meant pair, most frequent first."""
    return sorted(_load_corrections()["pairs"].values(), key=lambda p: -p["count"])


def _rules():
    try:
        mtime = os.path.getmtime(CORRECTIONS_FILE)
    except OSError:
        return []
    if mtime != _rules_cache["mtime"]:
        rules = []
        for p in corrections():
            if p["count"] >= config.VOICE_RULE_MIN:
                words = r"\W+".join(re.escape(w) for w in p["heard"].split())
                rules.append((re.compile(rf"\b{words}\b", re.IGNORECASE), p["meant"]))
        _rules_cache.update(mtime=mtime, rules=rules)
    return _rules_cache["rules"]


def apply_fixes(text):
    """Replace phrases you've corrected VOICE_RULE_MIN+ times ("this cord" → "Discord")."""
    for pattern, meant in _rules():
        text = pattern.sub(meant, text)
    return text


# ── Vocabulary ────────────────────────────────────────────────────────────────
def vocabulary():
    """Your words (vocabulary.txt) plus everything you've corrected Jarvis to, no duplicates."""
    words = []
    try:
        with open(VOCAB_FILE, encoding="utf-8") as f:
            words = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    except FileNotFoundError:
        pass
    words += [p["meant"] for p in corrections() if len(p["meant"]) > 2]
    seen, out = set(), []
    for w in words:
        if norm(w) not in seen:
            seen.add(norm(w))
            out.append(w)
    return out


def add_word(word):
    word = word.strip().strip(".")
    if not word or norm(word) in {norm(w) for w in vocabulary()}:
        return False
    _ensure()
    new = not os.path.exists(VOCAB_FILE)
    with open(VOCAB_FILE, "a", encoding="utf-8") as f:
        if new:
            f.write(_VOCAB_HEADER)
        f.write(word + "\n")
    return True
