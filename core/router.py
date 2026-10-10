"""
Local fast path: common, unambiguous commands are matched here and never reach
Claude — zero tokens and roughly a second faster. Anything not matched with
confidence returns None and goes to Claude as usual.
"""

import re
import time

from . import files, state

_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
                 "twenty": 20, "thirty": 30, "forty five": 45, "sixty": 60}
_UNIT_SECS = {"second": 1, "minute": 60, "hour": 3600}

# Things after "play" that are too vague to search for — let Claude decide
_VAGUE_PLAY = {"something", "some music", "music", "a song", "anything", "it", "that"}


def _normalize(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s']", " ", text)                    # Whisper adds punctuation
    text = re.sub(r"^(hey |ok |okay )?jarvis\b", "", text)
    text = re.sub(r"^(please|can you|could you)\b", "", text)
    text = re.sub(r"\b(please|for me|now)$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _music(command, **extra):
    return {"mode": "action", "actions": [{"type": "music", "command": command, **extra}]}


_MUSIC = [
    (r"(pause|pause (the )?(music|song))", lambda m: _music("pause")),
    (r"(resume|unpause|continue)( (the )?(music|song))?", lambda m: _music("resume")),
    (r"(skip|next)( (this|the))?( (song|track))?", lambda m: _music("skip")),
    (r"(stop|turn off|kill) (the )?music", lambda m: _music("stop")),
    (r"(play )?(the )?(previous|last) (song|track)|go back", lambda m: _music("prev", n=1)),
    (r"replay( (this|it|the song))?|play (it|that|this) again|restart (the |this )?song",
     lambda m: _music("replay")),
]


def _time_reply(text):
    if re.fullmatch(r"what('s| is) the time|what time is it", text):
        return time.strftime("It's %I:%M %p.").replace(" 0", " ")
    if re.fullmatch(r"what('s| is) (the date|today's date)|what day is it( today)?", text):
        return time.strftime("It's %A, %B %d.").replace(" 0", " ")
    return None


def route(command):
    """Return a response dict (same shape Claude returns) or None to defer to Claude."""
    text = _normalize(command)
    if not text:
        return None

    for pattern, build in _MUSIC:
        m = re.fullmatch(pattern, text)
        if m:
            return build(m)

    m = re.fullmatch(r"play (.+)", text)
    if m and m.group(1) not in _VAGUE_PLAY and not re.search(r"\b(on youtube|video|playlist|previous|last|again)\b", text):
        return _music("play", query=m.group(1))

    reply = _time_reply(text)
    if reply:
        return {"mode": "chat", "reply": reply}

    m = re.fullmatch(r"what('s| is| do i have)( on)? (my agenda|my schedule|due)( (for )?(today|tomorrow|this week))?"
                     r"|what do i have( on| due)? (today|tomorrow|this week)", text)
    if m:
        rng = next((w for w in ("today", "tomorrow") if w in text), "week")
        return {"mode": "action", "actions": [{"type": "agenda", "command": "list", "range": rng}]}
    if re.fullmatch(r"(give me |what's |what is )?(a |the |my )?(daily |morning )?(breakdown|briefing|brief|rundown)"
                    r"( (of|for) (today|the day))?|brief me|(give me )?(a |my )?(daily|morning) (briefing|brief|breakdown)"
                    r"|what does (today|my day) look like", text):
        return {"mode": "action", "actions": [{"type": "briefing"}]}
    if re.fullmatch(r"show (me )?my (agenda|schedule)", text):
        return {"mode": "action", "actions": [{"type": "agenda", "command": "show"}]}
    if re.fullmatch(r"what do you (remember|know) about me|show (me )?(my|your) memory", text):
        return {"mode": "action", "actions": [{"type": "memory", "command": "show"}]}

    m = re.fullmatch(r"set (a |an )?timer for (\d+|[a-z]+(?: five)?) (second|minute|hour)s?", text)
    if m:
        amount = int(m.group(2)) if m.group(2).isdigit() else _NUMBER_WORDS.get(m.group(2))
        if amount:
            return {"mode": "action", "actions": [
                {"type": "timer", "seconds": amount * _UNIT_SECS[m.group(3)], "label": ""}]}

    m = re.fullmatch(r"(open|launch|start) (up )?(.+)", text)
    if m:
        target = m.group(3)
        # Exact workspace name only — fuzzy names ("three eleven") still go to Claude
        for key in state.workspaces:
            if key.lower() == target:
                return {"mode": "action", "actions": [{"type": "workspace", "target": key}]}
        app = files.find_app(target)
        if app and len(target) >= 3 and app["name"].lower().startswith(target):
            return {"mode": "action", "actions": [{"type": "app", "target": app["name"]}]}

    return None
