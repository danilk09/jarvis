"""
Long-term memory: what Jarvis knows about the user, and what was said on past days.

  jarvis_memory/memory.md            facts, one bullet each, under ## sections. Edit it freely —
                                     every bullet goes into Claude's system prompt (cached block).
  jarvis_memory/conversations/*.jsonl  everything said, one file per day
  jarvis_memory/daily/*.md           a few sentences per past day (the last 3 go in the prompt)

Facts are added when the user asks ("remember that ...") and, after the conversation
has been quiet for config.MEMORY_REVIEW_IDLE_MIN minutes, by a background review that
picks out lasting facts the user mentioned in passing. Those lines end in "(auto)".
"""

import datetime as dt
import difflib
import json
import os
import re
import threading
import time

from . import config, stage, state, verbosity
from .registry import action
from .tts import speak

FACTS_FILE = os.path.join(config.MEMORY_DIR, "memory.md")
_LOG_DIR   = os.path.join(config.MEMORY_DIR, "conversations")
_DAILY_DIR = os.path.join(config.MEMORY_DIR, "daily")
_PROGRESS  = os.path.join(config.MEMORY_DIR, "review.json")

SECTIONS = {"about": "About me", "people": "People", "preferences": "Preferences",
            "places": "Places", "other": "Other"}
_AUTO = " (auto)"
_RECENT_DAYS = 3

_TEMPLATE = """# What Jarvis remembers

Everything in this file goes into Jarvis's prompt, so keep each fact short. Edit freely:
one fact per bullet, under any heading. Bullets ending in (auto) were picked up from
conversation rather than told to Jarvis directly.
""" + "".join(f"\n## {title}\n" for title in SECTIONS.values())

# Lines that carry no information for the conversation log
_FILLER = {"yes sir.", "ready for your command.", "let me look that up.", "on it.", "pulling it up.",
           "reading it now.", "i didn't catch that. try again."}

_lock  = threading.RLock()
_cache = {"mtime": None, "facts": [], "lines": []}


# ── Facts file ────────────────────────────────────────────────────────────────
def _ensure():
    os.makedirs(config.MEMORY_DIR, exist_ok=True)
    if not os.path.exists(FACTS_FILE):
        with open(FACTS_FILE, "w", encoding="utf-8") as f:
            f.write(_TEMPLATE)


def _section_key(heading):
    h = heading.strip().lower()
    for key, title in SECTIONS.items():
        if h == title.lower() or h.startswith(key):
            return key
    return h


def facts():
    """Every bullet in memory.md: [{"line", "section", "text", "auto"}], re-read when the file changes."""
    with _lock:
        _ensure()
        mtime = os.path.getmtime(FACTS_FILE)
        if mtime != _cache["mtime"]:
            with open(FACTS_FILE, encoding="utf-8") as f:
                lines = f.read().splitlines()
            out, section = [], "other"
            for i, line in enumerate(lines):
                m = re.match(r"\s*#{2,}\s*(.+)", line)
                if m:
                    section = _section_key(m.group(1))
                    continue
                m = re.match(r"\s*[-*+]\s+(.+)", line)
                if m and m.group(1).strip():
                    text = m.group(1).strip()
                    auto = text.endswith(_AUTO.strip())
                    out.append({"line": i, "section": section,
                                "text": text[:-len(_AUTO.strip())].strip() if auto else text, "auto": auto})
            _cache.update(mtime=mtime, facts=out, lines=lines)
        return [dict(f) for f in _cache["facts"]]


def _write(lines):
    tmp = FACTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    os.replace(tmp, FACTS_FILE)
    _cache["mtime"] = None
    _refresh_panel()


def _lines():
    facts()
    return list(_cache["lines"])


def _norm(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _duplicate(text, existing):
    n = _norm(text)
    return any(difflib.SequenceMatcher(None, n, _norm(f["text"])).ratio() > 0.88 for f in existing)


def add(text, section="other", auto=False):
    """Add a fact under its section (created if missing). Returns False if it's already known."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return False
    section = section if section in SECTIONS else _section_key(section or "other")
    with _lock:
        current = facts()
        if _duplicate(text, current):
            return False
        lines = _lines()
        bullet = f"- {text}{_AUTO if auto else ''}"
        heading = SECTIONS.get(section, section.title())
        in_section = [f["line"] for f in current if f["section"] == section]
        if in_section:
            lines.insert(max(in_section) + 1, bullet)
        else:
            at = next((i for i, l in enumerate(lines)
                       if re.match(r"\s*#{2,}\s*(.+)", l) and _section_key(l.lstrip("# ")) == section), None)
            if at is None:
                lines += ["", f"## {heading}", bullet]
            else:
                lines.insert(at + 1, bullet)
        _write(lines)
    print(f"  [memory] + {text}{_AUTO if auto else ''}")
    return True


def change(numbers_to_text):
    """Replace (text) or delete (None) facts by their number in prompt_block(). Returns how many changed."""
    with _lock:
        current = facts()
        lines = _lines()
        done = 0
        valid = [(current[n - 1], text) for n, text in numbers_to_text.items() if 1 <= n <= len(current)]
        for f, text in sorted(valid, key=lambda ft: -ft[0]["line"]):   # bottom up: line numbers stay valid
            if text is None:
                del lines[f["line"]]
                print(f"  [memory] - {f['text']}")
            else:
                indent = re.match(r"\s*[-*+]\s+", lines[f["line"]]).group(0)
                lines[f["line"]] = f"{indent}{text.strip()}"
                print(f"  [memory] ~ {f['text']} -> {text.strip()}")
            done += 1
        if done:
            _write(lines)
        return done


def find(description):
    """Best-matching fact number for a spoken description ("my sister's name"), or None."""
    current = facts()
    if not current or not description:
        return None
    words = set(_norm(description).split()) - {"the", "my", "a", "that", "is", "i", "me", "about"}
    best, best_score = None, 0.0
    for n, f in enumerate(current, 1):
        text = _norm(f["text"])
        score = difflib.SequenceMatcher(None, _norm(description), text).ratio() + \
            sum(1 for w in words if len(w) > 2 and w in text) * 0.25
        if score > best_score:
            best, best_score = n, score
    return best if best_score >= 0.5 else None


def _refresh_panel():
    """Keep memory.md up to date if it's open on the Stage (without switching to the Stage)."""
    for p in stage.panels():
        if p["kind"] == "file" and stage.private(p["id"]).get("path") == FACTS_FILE:
            with open(FACTS_FILE, encoding="utf-8") as f:
                content = f.read()
            stage.update(p["id"], content=content, version=p["data"].get("version", 0) + 1, proposal=None)


# ── What goes into the prompt ─────────────────────────────────────────────────
def _recent_days():
    if not os.path.isdir(_DAILY_DIR):
        return []
    names = sorted(n for n in os.listdir(_DAILY_DIR) if n.endswith(".md"))[-_RECENT_DAYS:]
    out = []
    for n in names:
        with open(os.path.join(_DAILY_DIR, n), encoding="utf-8") as f:
            out.append((n[:-3], f.read().strip()))
    return out


def prompt_block():
    """Facts and recent days for Claude's system prompt. Changes only when memory does, so it's cached."""
    current = facts()
    days = _recent_days()
    if not current and not days:
        return ""
    parts = []
    if current:
        parts.append("MEMORY — what you know about the user (numbered for the memory action). Use it "
                     "naturally when relevant; never recite it unprompted:\n" +
                     "\n".join(f"[{n}] ({f['section']}) {f['text']}" for n, f in enumerate(current, 1)))
    if days:
        parts.append("RECENT DAYS (summaries of earlier conversations):\n" +
                     "\n".join(f"{d}: {text}" for d, text in days))
    return "\n\n".join(parts)


# ── Conversation log ──────────────────────────────────────────────────────────
def log(role, text):
    """Append one line of conversation to today's log (state.push_log calls this)."""
    text = str(text or "").strip()
    if not text or text.lower() in _FILLER:
        return
    now = time.time()
    path = os.path.join(_LOG_DIR, time.strftime("%Y-%m-%d") + ".jsonl")
    with _lock:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now, "time": time.strftime("%H:%M:%S"), "role": role, "text": text}) + "\n")


def _read_log(day):
    path = os.path.join(_LOG_DIR, f"{day}.jsonl")
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _log_days():
    if not os.path.isdir(_LOG_DIR):
        return []
    return sorted(n[:-6] for n in os.listdir(_LOG_DIR) if n.endswith(".jsonl"))


def _transcript(entries, limit=12000):
    text = "\n".join(f"{'User' if e['role'] == 'user' else 'Jarvis'}: {e['text']}" for e in entries)
    return text[-limit:]


# ── Background review ─────────────────────────────────────────────────────────
_REVIEW_SYSTEM = """You maintain the long-term memory of JARVIS, a voice assistant, about its user.
You get the facts already saved (numbered) and a recent conversation. Return ONLY JSON:
{"ops":[...]} where each op is one of
{"op":"add","section":"about|people|preferences|places|other","fact":"<one short third-person sentence, e.g. The user's sister is named Ana.>"}
{"op":"update","n":<number>,"fact":"<the corrected fact>"}
{"op":"delete","n":<number>}
Save only LASTING facts the user stated about themselves: their life, work or school, people, places, preferences, routines and ongoing situations.
Skip: commands and one-off requests (play music, open an app, search for X), anything only Jarvis said, questions, guesses, moods and other temporary states, and anything already saved.
Deadlines, appointments and to-dos are handled by the agenda — skip them.
Never save passwords, codes, account or card numbers, addresses of other people, or keys.
Write dates as dates (e.g. "since September 2026"), never "recently" or "a month ago" — the fact must stay true as time passes.
Update or delete a saved fact only when the user clearly contradicted it.
Most conversations contain nothing worth saving: then return {"ops":[]}."""

_DAY_SYSTEM = ("Summarize one day of conversations between a user and JARVIS, their voice assistant, in 2-4 "
               "plain sentences: what the user worked on, asked about, planned or decided, and anything left "
               "unfinished. Write about 'the user'. Skip small talk and routine commands like playing music. "
               "If nothing meaningful happened, reply with just: Nothing notable.")


def _ask(system, content, max_tokens):
    from . import brain
    resp = config.client.messages.create(model=config.MODEL, max_tokens=max_tokens, system=system,
                                         messages=[{"role": "user", "content": content}])
    brain.log_usage("memory", resp.usage)
    return resp.content[0].text.strip()


def _progress():
    try:
        with open(_PROGRESS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"reviewed": 0.0}


def _save_progress(p):
    os.makedirs(config.MEMORY_DIR, exist_ok=True)
    with open(_PROGRESS, "w", encoding="utf-8") as f:
        json.dump(p, f)


def review(force=False):
    """Look through conversation since the last review for lasting facts. Runs once the
    conversation has been quiet for MEMORY_REVIEW_IDLE_MIN (or right away with force)."""
    from . import brain
    progress = _progress()
    since = progress.get("reviewed", 0.0)
    since_day = dt.date.fromtimestamp(since).isoformat() if since else ""
    entries = [e for d in _log_days() if d >= since_day for e in _read_log(d) if e.get("ts", 0) > since]
    if not entries:
        return 0
    if not force and time.time() - entries[-1]["ts"] < config.MEMORY_REVIEW_IDLE_MIN * 60:
        return 0
    if not any(e["role"] == "user" for e in entries):
        progress["reviewed"] = entries[-1]["ts"]
        _save_progress(progress)
        return 0
    current = facts()
    saved = "\n".join(f"[{n}] ({f['section']}) {f['text']}" for n, f in enumerate(current, 1)) or "(none yet)"
    try:
        raw = _ask(_REVIEW_SYSTEM, f"TODAY: {dt.date.today():%A %Y-%m-%d}\n\nSAVED FACTS:\n{saved}\n\n"
                                   f"CONVERSATION:\n{_transcript(entries)}", 600)
    except Exception as e:
        print(f"  [memory] review failed: {e}")
        return 0
    ops = (brain.parse_json(raw) or {}).get("ops", [])
    changes, added = {}, 0
    for op in ops if isinstance(ops, list) else []:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "add" and op.get("fact"):
            added += add(op["fact"], op.get("section", "other"), auto=True)
        elif kind in ("update", "delete") and str(op.get("n", "")).isdigit():
            n = int(op["n"])
            # only facts that were picked up automatically may be changed without asking
            if 1 <= n <= len(current) and current[n - 1]["auto"]:
                changes[n] = (op.get("fact", "").strip() + _AUTO) if kind == "update" and op.get("fact") else None
    # facts() numbering shifted if anything was added: re-find the originals by text
    if changes:
        now = facts()
        renumbered = {}
        for n, text in changes.items():
            old = current[n - 1]["text"]
            hit = next((i for i, f in enumerate(now, 1) if f["text"] == old), None)
            if hit:
                renumbered[hit] = text
        change(renumbered)
    progress["reviewed"] = entries[-1]["ts"]
    _save_progress(progress)
    return added + len(changes)


def summarize_days():
    """Write a summary for every past day that has a log but no summary yet."""
    today = dt.date.today().isoformat()
    for day in _log_days():
        path = os.path.join(_DAILY_DIR, f"{day}.md")
        if day >= today or os.path.exists(path):
            continue
        entries = _read_log(day)
        if not any(e["role"] == "user" for e in entries):
            continue
        try:
            text = _ask(_DAY_SYSTEM, _transcript(entries, 16000), 250)
        except Exception as e:
            print(f"  [memory] day summary failed: {e}")
            return
        os.makedirs(_DAILY_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"  [memory] summarized {day}")


def _loop():
    last_days = 0.0
    while True:
        try:
            if time.time() - last_days > 3600:
                summarize_days()
                last_days = time.time()
            review()
        except Exception as e:
            print(f"  [memory] background error: {e}")
        time.sleep(60)


def start():
    _ensure()
    state.on_log.append(log)
    threading.Thread(target=_loop, daemon=True, name="memory").start()


# ── Recall ────────────────────────────────────────────────────────────────────
def recall(question, days=60):
    """Answer a question about past conversations from the daily summaries and matching log lines."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    words = [w for w in _norm(question).split() if len(w) > 3]
    parts = []
    for day in _log_days():
        if day < cutoff:
            continue
        summary = os.path.join(_DAILY_DIR, f"{day}.md")
        if os.path.exists(summary):
            with open(summary, encoding="utf-8") as f:
                parts.append(f"## {day} summary: {f.read().strip()}")
        hits = [e for e in _read_log(day) if any(w in _norm(e["text"]) for w in words)]
        if hits:
            parts.append(f"## {day} matching lines:\n" + _transcript(hits[:20], 3000))
    context = "\n".join(parts)[-14000:]
    if not context:
        return "I don't have anything about that in our past conversations."
    return _ask("You answer questions about the user's past conversations with JARVIS, their voice assistant, "
                f"using only the notes below. Today is {dt.date.today():%A, %B %d, %Y}. If the notes don't "
                "say, say so. Plain spoken sentences, no markdown. " + verbosity.rule(),
                f"Question: {question}\n\nNotes:\n{context}", verbosity.tokens(250, 600))


# ── Voice ─────────────────────────────────────────────────────────────────────
@action("memory",
        schema='{"type":"memory","command":"remember|forget|update|show|recall","fact":"<for remember/update: the fact as one short third-person sentence, e.g. The user\'s sister is named Ana.>","section":"about|people|preferences|places|other","n":<fact number from MEMORY for forget/update, or 0>,"question":"<for recall>"}',
        rules=['"remember that X", "remember my X is Y", "don\'t forget that X", "my X is Y, remember that" (about the user\'s life — not coding) → memory "remember"; rewrite the fact in the third person',
               '"forget that X", "that\'s not true anymore" → memory "forget" with n; "actually X is Y now" about a saved fact → memory "update" with n and the corrected fact',
               '"what do you remember about me", "what do you know about me", "show my memory" → memory "show"',
               '"what did I ask you about X", "what did we talk about yesterday/last week", "when did I mention X" → memory "recall" with the question'])
def _memory(a, chain):
    cmd = a.get("command", "remember")
    if cmd == "remember":
        fact = (a.get("fact") or "").strip()
        if not fact:
            speak("What should I remember?")
            return "Memory: nothing to remember"
        speak("I'll remember that." if add(fact, a.get("section", "other")) else "I already knew that.")
        return f"Memory: + {fact}"
    if cmd in ("forget", "update"):
        n = int(a.get("n") or 0) if str(a.get("n") or "0").isdigit() else 0
        n = n or find(a.get("fact", "")) or 0
        if not n or n > len(facts()):
            speak("I couldn't find that in my memory.")
            return "Memory: no match"
        old = facts()[n - 1]["text"]
        if cmd == "forget":
            change({n: None})
            speak("Forgotten.")
            return f"Memory: - {old}"
        change({n: a.get("fact", old)})
        speak("Updated.")
        return f"Memory: {old} -> {a.get('fact')}"
    if cmd == "show":
        stage.open_file(FACTS_FILE)
        n = len(facts())
        speak(f"I remember {n} thing{'s' if n != 1 else ''} about you. They're on the stage — edit anything there."
              if n else "I don't remember anything about you yet. Tell me something and say remember that.")
        return "Memory: shown"
    if cmd == "recall":
        answer = recall(a.get("question") or a.get("fact") or "")
        speak(answer)
        return f"Memory recall: {answer[:80]}"
    speak("I'm not sure what to do with my memory there.")
    return f"Memory: unknown command {cmd}"
