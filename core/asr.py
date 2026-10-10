"""
Speech-to-text settings shared by Jarvis (core/audio.py) and scripts/voice/benchmark.py, so
the benchmark measures exactly what Jarvis does.

The hint prompt is the fixed command phrases plus your vocabulary (workspace names,
jarvis_memory/voice/vocabulary.txt and every word you've corrected Jarvis to). After
decoding, learned fixes ("this cord" → "Discord") are applied.
"""

import time

from . import config, speech_data

# Phrase order matters: noisy audio can make Whisper repeat this list, and strip_prompt_echo
# cuts runs of neighbouring phrases, so phrases you'd say together must not be neighbours.
_BASE_PHRASES = ["Jarvis", "open Discord", "search YouTube for", "open workspace", "never mind",
                 "take a screenshot", "find files", "yes", "analyze image", "cancel", "what's on screen",
                 "open file", "play", "no", "pause", "resume", "stop"]
_MAX_PROMPT_CHARS = 600            # Whisper keeps ~220 prompt tokens; stay well inside that
_cache = {"time": 0.0, "phrases": list(_BASE_PHRASES)}


def _names():
    """Workspace names: words Jarvis hears that no dictionary has. (Repo names aren't added —
    there are too many; put the ones Jarvis mishears in vocabulary.txt.)"""
    try:
        from . import state
        return list(state.workspaces)
    except Exception:
        return []


def phrases():
    """Prompt phrases in order (rebuilt at most every 30 s)."""
    if time.time() - _cache["time"] > 30:
        out, seen, size = list(_BASE_PHRASES), {p.lower() for p in _BASE_PHRASES}, len(", ".join(_BASE_PHRASES))
        extra = speech_data.vocabulary() + _names() if config.WHISPER_VOCAB_PROMPT else []
        for w in extra:
            w = w.replace("-", " ").replace("_", " ").strip()
            if w and w.lower() not in seen and size + len(w) + 2 <= _MAX_PROMPT_CHARS:
                out.append(w)
                seen.add(w.lower())
                size += len(w) + 2
        _cache.update(time=time.time(), phrases=out)
    return _cache["phrases"]


def strip_prompt_echo(text, prompt_phrases=None):
    """
    On noisy audio Whisper sometimes repeats its hint prompt ("…open Discord, search YouTube
    for, open workspace, never mind"). Cut the text where two or more prompt phrases follow
    each other in prompt order; a real "search YouTube for cats" is left alone.
    """
    known = [p.lower() for p in (prompt_phrases or phrases())]
    parts = [p.strip() for p in text.split(",")]
    for i in range(len(parts) - 1):
        a, b = parts[i].lower().rstrip(".!?"), parts[i + 1].lower().rstrip(".!?")
        # start at the 3rd phrase: "Jarvis, open Discord" is how real commands begin too
        for k in range(2, len(known) - 1):
            if a == known[k] and b and known[k + 1].startswith(b):
                return ", ".join(parts[:i]).strip(" ,")
    return text


def decode(model, audio, use_prompt=True, beam_size=None):
    """Raw transcript of a float32 array or a file path. Not thread-safe per model — lock outside."""
    prompt_phrases = phrases() if use_prompt else []
    segments, _ = model.transcribe(
        audio,
        language="en",
        beam_size=beam_size or config.WHISPER_BEAM,   # 1 = greedy: noticeably faster on CPU
        # No temperature fallback: on noisy audio Whisper otherwise re-decodes up to 6 times,
        # running off into repeated text — 20-40 s per command in a loud room, in testing
        temperature=0.0,
        condition_on_previous_text=False,
        no_repeat_ngram_size=3,
        max_new_tokens=80,
        without_timestamps=True,
        vad_filter=True,
        initial_prompt=", ".join(prompt_phrases) or None,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
    )
    # segments is a lazy generator — decoding happens here
    text = " ".join(s.text.strip() for s in segments).strip()
    return strip_prompt_echo(text, prompt_phrases) if prompt_phrases else text


def transcribe(model, audio, **kw):
    """(text with learned fixes applied, raw text)."""
    raw = decode(model, audio, **kw)
    return speech_data.apply_fixes(raw), raw
