"""
Check-ins: Jarvis asks how things went, the way a friend would ("How did the quiz go
yesterday?"), and remembers what matters from the answer.

A *moment* is an event in the user's life with real stakes: an exam, an interview, a
doctor's appointment, a trip, being sick. Moments come from two places:
  - the agenda: every dated one-off item is classified once by Haiku (WORTH_ASKING below).
    Recurring items, routine coursework, bills and chores never are.
  - conversation: the background memory review (memory.py) reports events mentioned in
    passing ("I have an interview at Google on Thursday") → add_moment()

After the moment and until CHECKIN_EXPIRE_DAYS later, Jarvis asks about it once (once more
later if nobody answered), and only at a natural point: the first command after a break of
CHECKIN_GAP_MIN, or small talk ("good morning", "how's it going"), after a light command:
never during a follow-up chain, a long answer or a coding run, and at most
CHECKIN_MAX_PER_DAY a day. The question ends in "?", so jarvis.py listens for the answer
without the wake word. Claude gets the check-in in its prompt, replies like a friend and
returns a "checkin" action with the outcome plus anything lasting ("needs to study overflow
more" → memory, section "Working on"), which it brings up again when it's relevant.

Saved in jarvis_memory/checkins.json:
  {"next_id": 2,
   "moments": [{"id": "c1", "agenda": "a3", "kind": "deadline", "text": "quiz",
                "question": "How did the quiz go {when}?", "date": "", "time": "", "after": "event",
                "asked": [<epoch>], "answered": <epoch or 0>, "outcome": "", "dismissed": false}],
   "agenda": {"a3": "c1", "a4": "no"},   # agenda items already classified
   "asked_days": {"2026-10-13": 1}}
Moments linked to an agenda item take their date from it, so moving the item moves the
check-in, and deleting or skipping it cancels it.
"""

import datetime as dt
import difflib
import json
import os
import re
import threading
import time

from . import agenda, brain, config, memory, state, tts, verbosity
from .registry import action
from .tts import speak

_FILE = os.path.join(config.MEMORY_DIR, "checkins.json")
_SPACING = 2 * 3600        # between two check-ins
_RETRY_AFTER = 6 * 3600    # an unanswered check-in may be asked once more, this much later
_PENDING_FOR = 300         # how long a just-asked check-in stays in Claude's prompt

# What a friend would ask about afterwards. Shared with the memory review.
WORTH_ASKING = """Worth asking about = something a friend who knew would genuinely ask "how did it go?" about afterwards, because it has real stakes or feelings for the user: exams, quizzes, midterms, finals and tests; job or internship interviews and offers; presentations, auditions, tryouts; competitions and games they play in; performances; medical or dental appointments and procedures; a first day at a job or school; a big project or application they worked hard on; trips, moving, dates; an important meeting with a boss, professor or advisor; family events; being sick or hurt.
NOT worth asking about: routine or recurring coursework (discussion posts, weekly homework, readings, labs), ordinary classes, lectures and meetings, bills, chores, errands, small to-dos and reminders, anything recurring."""

_CLASSIFY = """You decide whether JARVIS, a voice assistant that knows its user like a friend, should later ask how an agenda item went.
""" + WORTH_ASKING + """
Return ONLY JSON: {"ask": true|false, "question": "<if ask: one short natural question, with {when} where a time word fits, e.g. \\"How did the PSY 101 quiz go {when}?\\", \\"How was the interview with Google {when}?\\", \\"How did the dentist go {when}?\\">", "after": "event|next_day"}
"after": next_day for things that last the whole day (a trip, moving day); otherwise event.
When in doubt, false."""

# Small talk: a natural moment to ask, even right after another command
_SOCIAL = re.compile(r"\b(good (morning|afternoon|evening)|hey|hi|hello|how are you|how's it going|how is it going|"
                     r"how('s| is) your day|what's up|what is up|i'm (back|home)|i am (back|home)|how have you been)\b",
                     re.IGNORECASE)
# Commands short enough that a question afterwards doesn't get in the way
_LIGHT = {"music", "app", "workspace", "url", "bookmark", "timer", "agenda", "memory", "briefing", "search",
          "web_search", "weather", "wolfram", "stage", "vscode", "none"}
_QUESTION_WORDS = {"how", "did", "are", "is", "was", "were", "have", "has", "do", "what", "any"}

_lock = threading.RLock()
_data = {"next_id": 1, "moments": [], "agenda": {}, "asked_days": {}}
_pending = {"id": None, "line": "", "time": 0.0}   # the check-in just asked
_offer   = {"id": None}                            # the check-in to ask after the current command
_seen = {"last": 0.0, "gap": float("inf")}         # last user line, and the pause before the current one
_held = [False]                                    # something else will speak first (hold/release)
_deferred = {"id": None}                           # the check-in waiting for release()


# ── Storage ───────────────────────────────────────────────────────────────────
def load():
    global _data
    try:
        with open(_FILE, encoding="utf-8") as f:
            _data = {"next_id": 1, "moments": [], "agenda": {}, "asked_days": {}, **json.load(f)}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"  [checkins] could not read {_FILE} ({e}), starting empty.")


def _save():
    os.makedirs(config.MEMORY_DIR, exist_ok=True)
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, indent=1)
    os.replace(tmp, _FILE)


def _get(mid):
    return next((m for m in _data["moments"] if m["id"] == mid), None)


# ── When ──────────────────────────────────────────────────────────────────────
def _date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def when(m):
    """(date, time or None) of a moment, or (None, None) if it no longer happens."""
    if m.get("agenda"):
        item = agenda.get(m["agenda"])
        if not item or item.get("repeat") or not item.get("when"):
            return None, None
        occ = agenda.occurrences(item, dt.date.min, dt.date.max)
        if not occ or occ[0]["skipped"]:
            return None, None
        return occ[0]["date"], occ[0]["time"]
    return _date(m.get("date")), agenda._time(m.get("time"))


def _window(m):
    """(first moment to ask, last moment to ask), or None."""
    d, t = when(m)
    if not d:
        return None
    if m.get("after") == "next_day" or (t is None and m.get("kind") != "appointment"):
        start = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(7))   # all-day/untimed: the next morning
    elif t is None:
        start = dt.datetime.combine(d, dt.time(18))
    else:
        start = dt.datetime.combine(d, t) + dt.timedelta(hours=2)
    return start, dt.datetime.combine(d + dt.timedelta(days=config.CHECKIN_EXPIRE_DAYS), dt.time(23, 59))


def _ago(d, today):
    days = (today - d).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "on " + d.strftime("%A")
    return "last week"


def question(m, now=None):
    now = now or dt.datetime.now()
    d, _ = when(m)
    q = (m.get("question") or f"How did {m['text']} go {{when}}?").strip()
    q = q.replace("{when}", _ago(d, now.date()) if d else "")
    q = re.sub(r"\s+([?,.])", r"\1", re.sub(r"\s+", " ", q)).strip()
    return q if q.endswith("?") else q.rstrip(".") + "?"


def due(now=None):
    """Moments it's time to ask about, most recent event first."""
    now = now or dt.datetime.now()
    out = []
    with _lock:
        for m in _data["moments"]:
            if m.get("answered") or m.get("dismissed") or len(m.get("asked", [])) >= 2:
                continue
            if m.get("asked") and time.time() - m["asked"][-1] < _RETRY_AFTER:
                continue
            w = _window(m)
            if w and w[0] <= now <= w[1]:
                out.append(m)
    return sorted(out, key=lambda m: when(m)[0], reverse=True)


# ── Adding moments ────────────────────────────────────────────────────────────
_STOP = {"with", "the", "user", "about", "have", "their", "from", "this", "that", "for", "your", "my"}


def _similar(a, b):
    wa = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if len(w) > 3 and w not in _STOP}
    wb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 3 and w not in _STOP}
    return bool(wa & wb) or difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.6


def _match(text, d):
    return next((m for m in _data["moments"] if when(m)[0] == d and _similar(m["text"], text)), None)


def add_moment(text, date, time_="", question_="", after="event", kind="event"):
    """An event the user mentioned in conversation (from the memory review). Returns the moment or None."""
    d = _date(date)
    today = dt.date.today()
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text or not d or not today - dt.timedelta(days=config.CHECKIN_EXPIRE_DAYS) <= d <= today + dt.timedelta(days=90):
        return None
    with _lock:
        if _match(text, d):
            return None
        m = {"id": f"c{_data['next_id']}", "agenda": "", "kind": kind, "text": text, "question": question_ or "",
             "date": d.isoformat(), "time": time_ if agenda._time(time_) else "",
             "after": "next_day" if after == "next_day" else "event",
             "asked": [], "answered": 0, "outcome": "", "dismissed": False}
        _data["next_id"] += 1
        _data["moments"].append(m)
        _save()
    print(f"  [checkins] + {m['id']} {text} ({d})")
    return m


def _classify(item):
    """Haiku: is this agenda item worth asking about afterwards? → {"ask", "question", "after"}"""
    group = f" ({item['group']})" if item.get("group") else ""
    resp = config.client.messages.create(
        model=config.MODEL, **config.NO_THINK, max_tokens=120, system=_CLASSIFY,
        messages=[{"role": "user", "content": f"{item['kind']}: {item['text']}{group}, {item['when']}"}])
    brain.log_usage("checkins", resp.usage)
    return brain.parse_json(config.reply_text(resp)) or {}


def classify_agenda():
    """Classify every dated one-off agenda item not seen yet (one small Haiku call each)."""
    oldest = dt.date.today() - dt.timedelta(days=config.CHECKIN_EXPIRE_DAYS)
    for item in agenda.items():
        if item["id"] in _data["agenda"] or item["kind"] == "timer":
            continue
        d = _date(item.get("when"))
        if item.get("repeat") or not d or d < oldest:
            with _lock:
                _data["agenda"][item["id"]] = "no"
                _save()
            continue
        try:
            verdict = _classify(item)
        except Exception as e:
            print(f"  [checkins] classify failed: {e}")
            return
        with _lock:
            if not verdict.get("ask"):
                _data["agenda"][item["id"]] = "no"
            else:
                m = _match(item["text"], d)     # already heard about it in conversation: link it
                if m:
                    m.update(agenda=item["id"], kind=item["kind"], question=m["question"] or verdict.get("question", ""))
                else:
                    group = f" ({item['group']})" if item.get("group") else ""
                    m = {"id": f"c{_data['next_id']}", "agenda": item["id"], "kind": item["kind"],
                         "text": item["text"] + group, "question": str(verdict.get("question") or ""), "date": "", "time": "",
                         "after": "next_day" if verdict.get("after") == "next_day" else "event",
                         "asked": [], "answered": 0, "outcome": "", "dismissed": False}
                    _data["next_id"] += 1
                    _data["moments"].append(m)
                _data["agenda"][item["id"]] = m["id"]
                print(f"  [checkins] {item['text']} -> will ask: {m['question']}")
            _save()


def _prune():
    """Forget moments well past their window (answered ones stay a month for context)."""
    now = dt.datetime.now()
    with _lock:
        keep = []
        for m in _data["moments"]:
            d, _ = when(m)
            if d is None and m.get("agenda"):
                continue                                   # the agenda item was deleted or skipped
            age = (now.date() - d).days if d else 999
            if age <= (30 if m.get("answered") else config.CHECKIN_EXPIRE_DAYS + 7):
                keep.append(m)
        live = {i["id"] for i in agenda.items()}
        known = {k: v for k, v in _data["agenda"].items() if k in live}
        if len(keep) != len(_data["moments"]) or known != _data["agenda"]:
            _data["moments"], _data["agenda"] = keep, known
            _save()


# ── Asking ────────────────────────────────────────────────────────────────────
def _on_log(role, text):
    if role == "user":
        now = time.time()
        _seen["gap"] = now - _seen["last"] if _seen["last"] else float("inf")
        _seen["last"] = now


def hold():
    """Something else will speak shortly (the briefing's inbox summary): a check-in due after
    this turn waits for release() instead of talking over it."""
    _held[0] = True


def release():
    """The briefing's inbox summary has been spoken (or never came): ask the check-in held back.
    jarvis.py hears the question while idle and listens for the answer, as for any background question."""
    _held[0] = False
    m = _get(_deferred["id"]) if _deferred["id"] else None
    _deferred["id"] = None
    if m:
        _ask(m, spoke=True)


def before_turn(command, from_phone=False, followups=0):
    """jarvis.py, before a command is handled: is this a natural moment to check in afterwards?
    If so Claude is told (context()) to leave the question at the end to Jarvis."""
    _offer["id"] = None
    if not config.CHECKINS or from_phone or followups or verbosity.detailed:
        return
    if _seen["gap"] < config.CHECKIN_GAP_MIN * 60 and not _SOCIAL.search(command or ""):
        return
    with _lock:
        if _data["asked_days"].get(dt.date.today().isoformat(), 0) >= config.CHECKIN_MAX_PER_DAY:
            return
        last = max((t for m in _data["moments"] for t in m.get("asked", [])), default=0)
        if time.time() - last < _SPACING:
            return
        ready = due()
        if ready:
            _offer["id"] = ready[0]["id"]


def shape_reply(response):
    """With a check-in coming, a chat reply ending in its own question ("How about you?")
    loses that last sentence, so Jarvis asks one thing, not two. A reply that's only a
    question is left alone (and the check-in waits)."""
    if not _offer["id"] or not isinstance(response, dict) or response.get("mode") != "chat":
        return response
    reply = str(response.get("reply") or "").strip()
    parts = re.split(r"(?<=[.!])\s+", reply)
    if not reply.endswith("?") or len(parts) < 2:
        return response
    return {**response, "reply": " ".join(parts[:-1])}


def after_turn(response, turn_start=0.0):
    """jarvis.py, once the command has been handled: ask the check-in before_turn picked, if
    the turn was a light one and nothing else was asked. Returns the line spoken, or ""."""
    m, _offer["id"] = _get(_offer["id"]) if _offer["id"] else None, None
    if not m or tts.suppressed.is_set() or tts.last_question["time"] > turn_start:   # interrupted, or asked already
        return ""
    response = response or {}
    if response.get("mode") == "action":
        if not {a.get("type") for a in response.get("actions", []) if isinstance(a, dict)} <= _LIGHT:
            return ""
    elif response.get("mode") != "chat":
        return ""
    if _held[0]:
        _deferred["id"] = m["id"]
        return ""
    return _ask(m, spoke=tts.last_spoken[0] > turn_start)


def _ask(m, spoke):
    today = dt.date.today().isoformat()
    with _lock:
        q = question(m)
        m.setdefault("asked", []).append(time.time())
        _data["asked_days"] = {today: _data["asked_days"].get(today, 0) + 1}
        _save()
    first = q.split(" ", 1)[0]
    line = f"By the way, {first.lower()}{q[len(first):]}" if spoke and first.lower() in _QUESTION_WORDS else q
    _pending.update(id=m["id"], line=line, time=time.time())
    print(f"  [checkins] asking about {m['id']} {m['text']}")
    speak(line)
    return line


def _asked(question_text):
    if _pending["id"] and question_text == _pending["line"] and time.time() - _pending["time"] < _PENDING_FOR:
        return _get(_pending["id"])
    return None


def answered(question_text, answer):
    """jarvis.py: the user answered a question Jarvis asked. Records it if it was a check-in."""
    with _lock:
        m = _asked(question_text)
        if not m:
            return False
        m.update(answered=time.time(), outcome=answer[:300])   # Claude's "checkin" action refines it
        _save()
    _pending["time"] = time.time()   # stays in the prompt while Claude handles the answer
    return True


def unanswered(question_text, dismissed=False):
    """No answer (silence → may ask once more later) or "not now" (never again)."""
    with _lock:
        m = _asked(question_text)
        if m and dismissed:
            m["dismissed"] = True
            _save()
    if m:
        _pending["id"] = None


def spoken_name(m):
    """"Quiz (CS 101)" → "the CS 101 quiz", "Interview with Google" → "the interview with Google"."""
    text = m["text"]
    g = re.fullmatch(r"(.+?) \((.+)\)", text)
    if g:
        text = f"{g.group(2)} {g.group(1).lower()}"
    elif not text[:2].isupper():
        text = text[0].lower() + text[1:]
    return text if re.match(r"(my|your|the|a|an) ", text, re.IGNORECASE) else "the " + text


def tomorrow():
    """Moments happening tomorrow (for "good luck tomorrow" at good night)."""
    d = dt.date.today() + dt.timedelta(days=1)
    with _lock:
        return [m for m in _data["moments"] if when(m)[0] == d and not m.get("dismissed")]


# ── Claude's side ─────────────────────────────────────────────────────────────
def _phrase(m, now):
    d, t = when(m)
    if not d:
        return m["text"]
    o = {"date": d, "time": t}
    return f"{m['text']} {agenda.when_words(o, now)}"


def context():
    """Live prompt block: the check-in just asked, big things coming up, how recent ones went."""
    now = dt.datetime.now()
    lines = []
    with _lock:
        if _offer["id"]:
            lines.append("CHECK-IN NEXT: after your reply Jarvis asks the user a question of its own, so end your "
                         "reply without a question this time.")
        m = _get(_pending["id"]) if _pending["id"] and time.time() - _pending["time"] < _PENDING_FOR else None
        if m:
            d, _ = when(m)
            lines.append(f'CHECK-IN: you just asked "{_pending["line"]}" (check-in {m["id"]}: {m["text"]}'
                         f'{", " + d.isoformat() if d else ""}). If the user\'s message answers it → action "checkin" '
                         f'with id "{m["id"]}".')
        soon = []
        for x in _data["moments"]:
            d, t = when(x)
            if d and now.date() <= d <= now.date() + dt.timedelta(days=2) and \
                    dt.datetime.combine(d, t or dt.time(23, 59)) > now:
                soon.append(_phrase(x, now))
        if soon:
            lines.append("COMING UP that matters to the user: " + "; ".join(soon) + ". You may wish them luck "
                         "once, when it fits (it's that day, they mention it) — not every reply.")
        done = sorted((x for x in _data["moments"] if x.get("answered") and x.get("outcome")
                       and time.time() - x["answered"] < 14 * 86400), key=lambda x: -x["answered"])[:4]
        if done:
            lines.append("HOW RECENT THINGS WENT (the user told you): " + "; ".join(
                f"{x['text']} ({when(x)[0] or dt.date.fromtimestamp(x['answered']):%b %d}): {x['outcome']}"
                for x in done))
    return "\n\n" + "\n".join(lines) if lines else ""


@action("checkin",
        schema='{"type":"checkin","id":"<check-in id from CHECK-IN>","outcome":"<how it went, a few words>","remember":["<lasting or useful fact from the answer, third person with the month, e.g. The user wants to study overflow more for CS 101 (Oct 2026). Usually empty>"],"reply":"<what you say back>"}',
        rules=['The user answering the CHECK-IN question ("How did the quiz go?") → type "checkin" with its id. reply: react like a friend who cares, matching their tone (glad, sympathetic, impressed) in one or two short sentences. If they mention something to work on, you may offer one concrete bit of help as a question ("Want me to put a review session for overflow on your to-do list?"). remember: only what will matter later (what they want to improve, a result that changes plans, an offer, a decision), never just "it went well". Chain other actions after it if they also asked for something.'])
def _checkin(a, chain):
    with _lock:
        m = _get(a.get("id")) or _get(_pending["id"])
        if m:
            m["answered"] = m.get("answered") or time.time()
            if a.get("outcome"):
                m["outcome"] = str(a["outcome"]).strip()[:200]
            _save()
    _pending["id"] = None
    notes = a.get("remember") if isinstance(a.get("remember"), list) else []
    for fact in notes[:3]:
        if isinstance(fact, str) and fact.strip():
            memory.add(fact, "ongoing", auto=True)
    reply = (a.get("reply") or "").strip()
    if reply:
        speak(reply)
    return f"Check-in {m['id'] if m else '?'}: {a.get('outcome', '')}"


# ── Background ────────────────────────────────────────────────────────────────
def _loop():
    while True:
        try:
            classify_agenda()
            _prune()
        except Exception as e:
            print(f"  [checkins] background error: {e}")
        time.sleep(60)


def start():
    load()
    _seen["last"] = memory.last_user_time()
    state.on_log.append(_on_log)
    brain.context_providers.append(context)
    threading.Thread(target=_loop, daemon=True, name="checkins").start()
