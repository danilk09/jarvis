"""
The agenda: deadlines, appointments, to-dos and timers, with spoken reminders that
survive restarts. Saved in jarvis_memory/agenda.json.

Recurring items are one rule, not one entry per week:
    {"id": "a3", "kind": "deadline", "text": "PSY 101 discussion post", "group": "PSY 101",
     "repeat": {"every": "week", "days": ["wed"], "time": "23:59",
                "from": "2026-08-24", "until": "2026-12-12", "interval": 1},
     "remind": ["1d@19:00", "0d@09:00"], "done": ["2026-10-07"], "skip": ["2026-11-25"]}
Each occurrence is identified by its date ("2026-10-14"), so "I submitted it" marks
only this week's. One-off items use "when" ("2026-10-13T15:00" or "2026-10-13")
instead of "repeat"; undated to-dos have neither.

Reminder specs: "30m" / "2h" / "1d" before, or "1d@19:00" = 7 PM the day before
("0d@09:00" = 9 AM the same day). Defaults per kind: config.AGENDA_REMIND_DEFAULTS.
"""

import datetime as dt
import json
import os
import re
import threading
import time

from . import config, events, notify, stage, state
from .registry import action
from .tts import speak

_FILE = os.path.join(config.MEMORY_DIR, "agenda.json")
DAYS  = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
KINDS = ("deadline", "appointment", "task", "timer")
_GRACE = 15 * 60          # a reminder missed by up to this much (busy, restarting) still fires
_OVERDUE_DAYS = 14

_lock = threading.RLock()
_data = {"next_id": 1, "items": [], "fired": {}}   # fired: reminder key -> epoch it fired
_wake = threading.Event()                          # an item changed: recompute the next reminder


# ── Storage ───────────────────────────────────────────────────────────────────
def load():
    global _data
    try:
        with open(_FILE, encoding="utf-8") as f:
            _data = {"next_id": 1, "items": [], "fired": {}, **json.load(f)}
        # timers that ran out long before this start are dropped, not announced late
        _data["items"] = [i for i in _data["items"]
                          if i["kind"] != "timer" or i.get("at", 0) > time.time() - _GRACE]
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"  Could not read the agenda ({e}) — starting empty.")


def _save():
    os.makedirs(config.MEMORY_DIR, exist_ok=True)
    cutoff = time.time() - 40 * 86400
    _data["fired"] = {k: v for k, v in _data["fired"].items() if v > cutoff}
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, indent=1)
    os.replace(tmp, _FILE)
    _wake.set()
    _refresh_panel()


def items():
    with _lock:
        return [dict(i) for i in _data["items"]]


def get(item_id):
    with _lock:
        return next((i for i in _data["items"] if i["id"] == item_id), None)


# ── Dates ─────────────────────────────────────────────────────────────────────
def _date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _time(s):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return dt.time(int(m.group(1)), int(m.group(2)))


def _days(raw):
    out = []
    for d in raw or []:
        key = str(d).strip().lower()[:3]
        if key in DAYS and key not in out:
            out.append(key)
    return out


def _repeat_dates(rep, start, end):
    """Dates in [start, end] on which a recurring rule falls."""
    frm = _date(rep.get("from")) or start
    until = _date(rep.get("until"))
    every = rep.get("every", "week")
    n = max(1, int(rep.get("interval") or 1))
    days = [DAYS.index(d) for d in _days(rep.get("days"))] or [frm.weekday()]
    week0 = frm - dt.timedelta(days=frm.weekday())
    d, hi = max(start, frm), min(end, until) if until else end
    while d <= hi:
        if every == "day":
            ok = (d - frm).days % n == 0
        elif every == "month":
            ok = d.day == frm.day and ((d.year - frm.year) * 12 + d.month - frm.month) % n == 0
        else:
            ok = d.weekday() in days and ((d - week0).days // 7) % n == 0
        if ok:
            yield d
        d += dt.timedelta(days=1)


def occurrences(item, start, end):
    """[{"key", "date", "time", "at"}] for one item between two dates. `at` is the due moment
    (end of day for all-day items). Done and skipped occurrences are included, flagged."""
    rep = item.get("repeat")
    if rep:
        t = _time(rep.get("time"))
        dates = list(_repeat_dates(rep, start, end))
    else:
        when = item.get("when") or ""
        d = _date(when)
        if not d or not start <= d <= end:
            return []
        t = _time(when[11:16]) if len(when) > 10 else None
        dates = [d]
    out = []
    for d in dates:
        key = d.isoformat()
        out.append({"key": key, "date": d, "time": t,
                    "at": dt.datetime.combine(d, t or dt.time(23, 59)),
                    "done": key in item.get("done", []), "skipped": key in item.get("skip", [])})
    return out


def upcoming(days=7, now=None):
    """Open (not done/skipped) occurrences from now through `days` days ahead, soonest first."""
    now = now or dt.datetime.now()
    out = []
    for item in items():
        if item["kind"] == "timer":
            continue
        for o in occurrences(item, now.date(), now.date() + dt.timedelta(days=days)):
            if not o["done"] and not o["skipped"] and (o["at"] >= now or o["time"] is None):
                out.append((o, item))
    return sorted(out, key=lambda oi: oi[0]["at"])


def _tracked(item, o):
    """Occurrences due before the item was added (earlier weeks of a semester) never count as missed."""
    created = item.get("created")
    return not created or o["at"] >= dt.datetime.fromisoformat(created)


def overdue(now=None):
    """Deadlines and to-dos whose due time passed in the last two weeks without being marked done."""
    now = now or dt.datetime.now()
    out = []
    for item in items():
        if item["kind"] not in ("deadline", "task"):
            continue
        for o in occurrences(item, now.date() - dt.timedelta(days=_OVERDUE_DAYS), now.date()):
            if o["at"] < now and not o["done"] and not o["skipped"] and _tracked(item, o) and \
                    not (o["time"] is None and o["date"] == now.date()):
                out.append((o, item))
    return sorted(out, key=lambda oi: oi[0]["at"])


def undated():
    return [i for i in items() if i["kind"] == "task" and not i.get("repeat") and not i.get("when")
            and not i.get("done")]


# ── Words ─────────────────────────────────────────────────────────────────────
def _clock(t):
    if t is None:
        return ""
    if t == dt.time(23, 59):
        return "midnight"
    if t == dt.time(12, 0):
        return "noon"
    s = dt.datetime.combine(dt.date.today(), t).strftime("%I:%M %p").lstrip("0")
    return s.replace(":00 ", " ")


def when_words(o, now=None):
    """"today at 3 PM", "tomorrow", "Wednesday at midnight", "October 20"."""
    today = (now or dt.datetime.now()).date()
    d = o["date"]
    delta = (d - today).days
    if delta == 0:
        day = "today" if not o["time"] or o["time"] < dt.time(18) else "tonight"
    elif delta == 1:
        day = "tomorrow"
    elif delta == -1:
        day = "yesterday"
    elif 1 < delta < 7:
        day = d.strftime("%A")
    elif -7 < delta < 0:
        day = "last " + d.strftime("%A")
    else:
        day = "on " + d.strftime("%B %d").replace(" 0", " ")
    return f"{day} at {_clock(o['time'])}" if o["time"] else day


def describe(item):
    """Spoken description of an item's schedule, for read-backs."""
    rep = item.get("repeat")
    if rep:
        every = rep.get("every", "week")
        n = int(rep.get("interval") or 1)
        if every == "week":
            names = [dt.date(2024, 1, 1 + DAYS.index(d)).strftime("%A") for d in _days(rep.get("days"))]
            names = names or [(_date(rep.get("from")) or dt.date.today()).strftime("%A")]
            days = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            sched = f"every {'other ' if n == 2 else ''}{days}"
        elif every == "day":
            sched = "every day" if n == 1 else f"every {n} days"
        else:
            sched = "every month on the " + _ordinal((_date(rep.get("from")) or dt.date.today()).day)
        t = _time(rep.get("time"))
        if t:
            sched += f" at {_clock(t)}"
        first = next(_repeat_dates(rep, dt.date.today(), dt.date.today() + dt.timedelta(days=62)), None)
        if first and (n > 1 or first > dt.date.today() + dt.timedelta(days=7)):
            sched += " starting " + first.strftime("%B %d").replace(" 0", " ")
        until = _date(rep.get("until"))
        if until:
            sched += " until " + until.strftime("%B %d").replace(" 0", " ")
        return sched
    if item.get("when"):
        o = occurrences(item, dt.date.min, dt.date.max)
        return when_words(o[0]) if o else item["when"]
    return "no date"


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _due_phrase(o, item, now=None):
    w = when_words(o, now)
    if item["kind"] == "deadline":
        return f"{item['text']}, due {w}"
    return f"{item['text']} {w}" if o["time"] else f"{item['text']}, {w}"


# ── Changing the agenda ───────────────────────────────────────────────────────
def _clean_repeat(rep):
    if not isinstance(rep, dict) or not rep:
        return None
    every = rep.get("every") if rep.get("every") in ("day", "week", "month") else "week"
    out = {"every": every, "days": _days(rep.get("days")), "time": rep.get("time") if _time(rep.get("time")) else "",
           "from": rep.get("from") if _date(rep.get("from")) else dt.date.today().isoformat(),
           "until": rep.get("until") if _date(rep.get("until")) else "",
           "interval": max(1, int(rep.get("interval") or 1)) if str(rep.get("interval") or 1).isdigit() else 1}
    if every == "week" and not out["days"]:
        out["days"] = [DAYS[_date(out["from"]).weekday()]]
    return out


def _clean_when(when):
    when = str(when or "").strip().replace(" ", "T")
    if not _date(when):
        return ""
    t = _time(when[11:16]) if len(when) > 10 else None
    return when[:10] + (f"T{t:%H:%M}" if t else "")


def _clean_remind(remind, kind):
    if not isinstance(remind, list):
        return list(config.AGENDA_REMIND_DEFAULTS.get(kind, []))
    return [str(r).strip().lower() for r in remind if re.fullmatch(r"\d+[mhd](@\d{1,2}:\d{2})?", str(r).strip().lower())]


def add(kind, text, when="", repeat=None, remind=None, group=""):
    kind = kind if kind in KINDS else "task"
    with _lock:
        item = {"id": f"a{_data['next_id']}", "kind": kind, "text": text.strip(), "group": (group or "").strip(),
                "when": "" if repeat else _clean_when(when), "repeat": _clean_repeat(repeat),
                "remind": _clean_remind(remind if remind else None, kind), "done": [], "skip": [],
                "created": dt.datetime.now().isoformat(timespec="seconds")}
        _data["next_id"] += 1
        _data["items"].append(item)
        _save()
    print(f"  [agenda] + {item['id']} {item['text']} ({describe(item)})")
    return item


def update(item_id, **fields):
    with _lock:
        item = get(item_id)
        if not item:
            return None
        for k, v in fields.items():
            if v in (None, "", [], {}):
                continue
            if k == "repeat":
                item["repeat"] = _clean_repeat({**(item.get("repeat") or {}), **v})
                item["when"] = ""
            elif k == "when":
                item["when"], item["repeat"] = _clean_when(v), None
            elif k == "remind":
                item["remind"] = _clean_remind(v, item["kind"])
            elif k == "kind" and v in KINDS:
                item["kind"] = v
            elif k in ("text", "group"):
                item[k] = str(v).strip()
        _save()
        return dict(item)


def remove(item_ids):
    with _lock:
        before = len(_data["items"])
        _data["items"] = [i for i in _data["items"] if i["id"] not in item_ids]
        if len(_data["items"]) != before:
            _save()
        return before - len(_data["items"])


def _current_occurrence(item, now=None):
    """The occurrence "it" means: the earliest open one that's overdue or due within a week."""
    now = now or dt.datetime.now()
    occ = occurrences(item, now.date() - dt.timedelta(days=_OVERDUE_DAYS), now.date() + dt.timedelta(days=7))
    open_ = [o for o in occ if not o["done"] and not o["skipped"] and _tracked(item, o)]
    return open_[0] if open_ else None


def mark(item_id, field, date=""):
    """Mark an occurrence done (field "done") or skipped ("skip"). Returns the occurrence, or None."""
    with _lock:
        item = get(item_id)
        if not item:
            return None
        if not item.get("repeat") and not item.get("when"):       # undated to-do
            item["done"] = ["any"]
            _save()
            return {"key": "any", "date": dt.date.today(), "time": None}
        d = _date(date)
        o = (next(iter(occurrences(item, d, d)), None) if d else None) or _current_occurrence(item)
        if not o:
            return None
        if o["key"] not in item[field]:
            item[field].append(o["key"])
        _save()
        return o


def unmark(item_id, date=""):
    with _lock:
        item = get(item_id)
        if not item or not item["done"]:
            return False
        key = date[:10] if _date(date) else item["done"][-1]
        item["done"] = [k for k in item["done"] if k != key]
        _save()
        return True


def snooze(item_id, occ_key, minutes=60):
    """Remind again about one occurrence in `minutes` (the phone's Snooze button)."""
    with _lock:
        item = get(item_id)
        if not item:
            return False
        key = occ_key if _date(occ_key) else (_current_occurrence(item) or {}).get("key")
        if not key:
            return False
        item.setdefault("snooze", {}).setdefault(key, []).append(time.time() + minutes * 60)
        _save()
        return True


def add_timer(seconds, label=""):
    when = dt.datetime.now() + dt.timedelta(seconds=seconds)
    with _lock:
        item = {"id": f"a{_data['next_id']}", "kind": "timer", "text": label, "group": "",
                "at": when.timestamp(), "when": "", "repeat": None, "remind": [], "done": [], "skip": [],
                "created": dt.datetime.now().isoformat(timespec="seconds")}
        _data["next_id"] += 1
        _data["items"].append(item)
        _save()
    return item


# ── Reminders ─────────────────────────────────────────────────────────────────
def _fire_time(spec, o):
    m = re.fullmatch(r"(\d+)([mhd])(?:@(\d{1,2}:\d{2}))?", spec)
    if not m:
        return None
    n, unit, at = int(m.group(1)), m.group(2), _time(m.group(3)) if m.group(3) else None
    if unit == "d" and at:
        fire = dt.datetime.combine(o["date"] - dt.timedelta(days=n), at)
        return fire if fire < o["at"] else None
    if o["time"] is None and unit != "d":
        return None          # "30 minutes before" an all-day item means nothing
    return o["at"] - dt.timedelta(minutes=n * {"m": 1, "h": 60, "d": 1440}[unit])


def _reminders(now):
    """Every reminder due between now - _GRACE and two days from now:
    [(fire datetime, key, message, item, occurrence or None)]."""
    out = []
    for item in items():
        if item["kind"] == "timer":
            at = dt.datetime.fromtimestamp(item["at"])
            label = f": {item['text']}" if item["text"] else ""
            out.append((at, f"{item['id']}|timer", f"Time's up{label}.", item, None))
            continue
        for o in occurrences(item, now.date() - dt.timedelta(days=1), now.date() + dt.timedelta(days=9)):
            if o["done"] or o["skipped"]:
                continue
            snoozed = [(dt.datetime.fromtimestamp(t), f"snooze{int(t)}")
                       for t in item.get("snooze", {}).get(o["key"], [])]
            for fire, spec in [(_fire_time(s, o), s) for s in item.get("remind", [])] + snoozed:
                if fire is None or fire > now + dt.timedelta(days=2) or fire < now - dt.timedelta(seconds=_GRACE):
                    continue
                if item["kind"] == "deadline":
                    msg = f"Reminder, sir: {item['text']} is due {when_words(o, fire)}."
                elif item["kind"] == "appointment" and o["time"]:
                    mins = round((o["at"] - fire).total_seconds() / 60)
                    soon = f"in {mins} minutes" if 0 < mins <= 90 else when_words(o, fire)
                    msg = f"Reminder, sir: {item['text']} {soon}."
                else:
                    msg = f"Reminder, sir: {item['text']}."
                out.append((fire, f"{item['id']}|{o['key']}|{spec}", msg, item, o))
    return sorted(out, key=lambda r: r[0])


def _idle():
    with state.dash_lock:
        return state.dash_state["state"] == "idle"


def _loop():
    while True:
        now = dt.datetime.now()
        pending = [r for r in _reminders(now) if r[1] not in _data["fired"]]
        due = [r for r in pending if r[0] <= now]
        away = notify.away()
        if due and not away and not _idle():   # don't talk over a command: try again shortly
            _wake.wait(3)
            _wake.clear()
            continue
        for fire, key, msg, item, o in due:
            # at the PC: say it. Away: send it to the phone. A deadline close to due: both.
            urgent = (bool(o) and item["kind"] == "deadline"
                      and (o["at"] - now).total_seconds() <= config.PHONE_URGENT_HOURS * 3600)
            if not away:
                speak(msg)
            if away or urgent:
                notify.reminder(msg, item, o["key"] if o else None)
            with _lock:
                _data["fired"][key] = time.time()
                if item["kind"] == "timer":
                    _data["items"] = [i for i in _data["items"] if i["id"] != item["id"]]
                _save()
        later = [r[0] for r in pending if r[0] > now]
        wait = min([(later[0] - now).total_seconds()] if later else [60], default=60)
        _wake.wait(max(0.5, min(60, wait)))
        _wake.clear()


def start():
    load()
    threading.Thread(target=_loop, daemon=True, name="agenda").start()


# ── Prompt context and the Stage ──────────────────────────────────────────────
def context():
    """Live block for Claude's prompt: the current time and what's on the agenda."""
    now = dt.datetime.now()
    lines = [f"\n\nNOW: {now:%A %Y-%m-%d %H:%M} (resolve relative dates from this)",
             "Holidays ahead: " + "; ".join(f"{name} {d:%a %Y-%m-%d}" for d, name in events.upcoming(60))]
    active = items()
    if active:
        lines.append("AGENDA (id: item — schedule):")
        for item in active[:40]:
            if item["kind"] == "timer":
                left = int(item["at"] - time.time())
                lines.append(f"{item['id']}: timer {item['text'] or ''} — {max(0, left) // 60} min {max(0, left) % 60} s left")
            else:
                group = f" [{item['group']}]" if item.get("group") else ""
                lines.append(f"{item['id']}: {item['kind']} {item['text']}{group} — {describe(item)}")
        soon = [f"{_due_phrase(o, i)} ({i['id']}, {o['key']})" for o, i in upcoming(7)[:12]]
        late = [f"{_due_phrase(o, i)} ({i['id']}, {o['key']})" for o, i in overdue()[:8]]
        if late:
            lines.append("Overdue (not marked done): " + "; ".join(late))
        if soon:
            lines.append("Next 7 days: " + "; ".join(soon))
    return "\n".join(lines)


def markdown():
    now = dt.datetime.now()
    out = ["_Change it by voice: \"add…\", \"I finished…\", \"skip … this week\", \"delete…\"._", ""]

    def section(title, rows):
        if rows:
            out.extend([f"### {title}", *rows, ""])

    section("Overdue", [f"- **{i['text']}** — was due {when_words(o, now)}" for o, i in overdue(now)])
    up = upcoming(7, now)
    section("Today", [f"- {('**' + _clock(o['time']) + '** ') if o['time'] else ''}{i['text']}"
                      f"{' _(' + i['group'] + ')_' if i.get('group') else ''}"
                      for o, i in up if o["date"] == now.date()])
    section("Next 7 days", [f"- {when_words(o, now)} — {i['text']}{' _(' + i['group'] + ')_' if i.get('group') else ''}"
                            for o, i in up if o["date"] != now.date()])
    section("To-do", [f"- {i['text']}" for i in undated()])
    section("Recurring", [f"- {i['text']} — {describe(i)}" for i in items() if i.get("repeat")])
    timers = [i for i in items() if i["kind"] == "timer"]
    section("Timers", [f"- {i['text'] or 'Timer'} — {dt.datetime.fromtimestamp(i['at']):%I:%M %p}" for i in timers])
    if len(out) == 2:
        out.append("Nothing on the agenda.")
    return "\n".join(out)


def _panel():
    return next((p["id"] for p in stage.panels() if p["kind"] == "note" and stage.private(p["id"]).get("agenda")), None)


def show():
    pid = _panel()
    if pid:
        stage.update(pid, markdown=markdown())
        stage.navigate("/stage")
    else:
        stage.add("note", "Agenda", {"markdown": markdown()}, private={"agenda": True})


def _refresh_panel():
    pid = _panel()
    if pid:
        stage.update(pid, markdown=markdown())


# ── Voice ─────────────────────────────────────────────────────────────────────
def _confirm(question):
    from .agent import confirm
    return confirm(question)


def _ask_date(question):
    """Ask for a date out loud and turn the answer into YYYY-MM-DD (None if unclear)."""
    from . import audio, brain
    from .tts import wait_tts
    speak(question)
    wait_tts()
    answer = audio.listen_for_command(max_duration=6, silence_duration=0.8)
    if not answer:
        return None
    try:
        resp = config.client.messages.create(
            model=config.MODEL, max_tokens=40,
            system=f"Today is {dt.date.today():%A %Y-%m-%d}. Return ONLY the date the user means as "
                   "YYYY-MM-DD, or NONE.", messages=[{"role": "user", "content": answer}])
        raw = resp.content[0].text.strip()
    except Exception:
        return None
    brain.log_usage("agenda", resp.usage)
    d = _date(re.sub(r"[^\d-]", "", raw))
    return d.isoformat() if d else None


def _ids_for(a):
    if a.get("id") and get(a["id"]):
        return [a["id"]]
    group = (a.get("group") or "").strip().lower()
    if group:
        return [i["id"] for i in items() if i.get("group", "").lower() == group]
    return []


def _speak_list(rng):
    now = dt.datetime.now()
    days = {"today": 0, "tomorrow": 1, "week": 7}.get(rng, 7)
    up = upcoming(max(days, 1), now)
    if rng == "today":
        up = [oi for oi in up if oi[0]["date"] == now.date()]
    elif rng == "tomorrow":
        up = [oi for oi in up if oi[0]["date"] == now.date() + dt.timedelta(days=1)]
    late = overdue(now)
    parts = []
    if late:
        parts.append("Overdue: " + "; ".join(_due_phrase(o, i, now) for o, i in late[:4]) + ".")
    if up:
        parts.append("; ".join(_due_phrase(o, i, now) for o, i in up[:8]) + ".")
    if not parts:
        label = {"today": "today", "tomorrow": "tomorrow"}.get(rng, "this week")
        return f"Nothing on the agenda {label}."
    return " ".join(parts)


@action("agenda",
        schema='{"type":"agenda","command":"add|done|undo_done|skip|remove|update|list|show","id":"<item id from AGENDA, for anything but add/list/show>","group":"<class or category, e.g. PSY 101, or empty>","kind":"deadline|appointment|task","text":"<short name>","when":"<YYYY-MM-DDTHH:MM, or YYYY-MM-DD, or empty>","repeat":{"every":"day|week|month","days":["mon"],"time":"<HH:MM or empty>","from":"<YYYY-MM-DD or empty>","until":"<YYYY-MM-DD, ask, or empty>","interval":1},"remind":["<e.g. 30m, 2h, 1d@19:00 — only if the user asked for specific reminders>"],"date":"<YYYY-MM-DD occurrence for done/skip, or empty for the current one>","range":"today|tomorrow|week"}',
        rules=['"I have X on <day> at <time>", "add X to my agenda", "X is due <date>", "remind me to X on <day>/at <time>" → agenda "add". kind: deadline = something due (assignment, bill), appointment = somewhere to be, task = a to-do. Resolve dates from NOW; times are 24-hour HH:MM; due "at midnight" / "by end of day" = "23:59". repeat null for one-off items.',
               'Recurring items ("every Wednesday", "each week", "every other Monday", "daily") → agenda "add" with repeat and "when" empty. "for this semester"/"until the semester ends": until = the semester end date from MEMORY, or "ask" if MEMORY doesn\'t have it.',
               '"I did/submitted/finished X", "mark X done", "X is done" → agenda "done" with its id (date empty = the current one); "I didn\'t actually finish X" → "undo_done"; "no X this week", "skip X on <date>" → "skip"',
               '"delete/cancel X", "remove X from my agenda", "clear everything for <class>" → agenda "remove" with id, or group for everything in it; moving or renaming an item → "update" with its id and only the changed fields',
               '"what\'s on my agenda", "what do I have today/tomorrow/this week", "what\'s due", "anything due soon" → agenda "list" with range; "show my agenda/schedule" → agenda "show"',
               '"remind me in N minutes to X" / "set a timer" stay type "timer" (they live on the agenda too).'])
def _agenda(a, chain):
    cmd = a.get("command", "list")
    rep = a.get("repeat") if isinstance(a.get("repeat"), dict) and a.get("repeat") else None
    if a.get("until"):                       # sometimes given next to "repeat" instead of inside it
        rep = {**(rep or {}), "until": a["until"]}
    if rep and str(rep.get("until", "")).lower() == "ask":
        until = _ask_date("When does the semester end?")
        rep["until"] = until or ""
        if until:
            from . import memory
            memory.add(f"The user's current semester ends on {until}.", "about")
    if cmd == "add":
        text = (a.get("text") or "").strip()
        if not text:
            speak("What should I add?")
            return "Agenda: nothing to add"
        item = add(a.get("kind", "task"), text, a.get("when", ""), rep, a.get("remind") or None, a.get("group", ""))
        if rep:
            if not _confirm(f"{text}, {describe(item)}"):
                remove([item["id"]])
                speak("Okay, I didn't save it.")
                return "Agenda: add cancelled"
            speak("Saved." + ("" if item["repeat"].get("until") else " It repeats until you tell me to stop."))
        elif item["kind"] == "task" and not item["when"]:
            speak(f"Added {text} to your to-do list.")
        else:
            speak(f"Got it: {text}, {describe(item)}.")
        return f"Agenda: + {item['id']} {text}"
    if cmd in ("done", "skip"):
        ids = _ids_for(a)
        if not ids:
            speak("I couldn't find that on your agenda.")
            return f"Agenda {cmd}: no match"
        o = mark(ids[0], "done" if cmd == "done" else "skip", a.get("date", ""))
        item = get(ids[0])
        if not o:
            speak(f"There's nothing open for {item['text']} right now.")
            return f"Agenda {cmd}: nothing open"
        if cmd == "done":
            speak(f"Nice work. {item['text']} is marked done." if o["key"] == "any"
                  else f"Marked {item['text']} for {when_words(o)} done.")
        else:
            speak(f"Skipping {item['text']} {when_words(o)}.")
        return f"Agenda {cmd}: {ids[0]} {o['key']}"
    if cmd == "undo_done":
        ids = _ids_for(a)
        ok = bool(ids) and unmark(ids[0], a.get("date", ""))
        speak("Okay, it's open again." if ok else "I couldn't find that one.")
        return f"Agenda undo_done: {ok}"
    if cmd == "remove":
        ids = _ids_for(a)
        names = [get(i)["text"] for i in ids]
        if not ids:
            speak("I couldn't find that on your agenda.")
            return "Agenda remove: no match"
        if len(ids) > 1 and not _confirm(f"That removes {len(ids)} items from {a.get('group')}"):
            speak("Okay, nothing removed.")
            return "Agenda remove: cancelled"
        remove(ids)
        speak(f"Removed {names[0]}." if len(ids) == 1 else f"Removed {len(ids)} items.")
        return f"Agenda: - {ids}"
    if cmd == "update":
        ids = _ids_for(a)
        # an "until" alone only makes sense for something that already repeats
        if rep and not rep.get("every") and not (ids and get(ids[0]).get("repeat")):
            rep = None
        item = update(ids[0], kind=a.get("kind"), text=a.get("text"), group=a.get("group"),
                      when=a.get("when"), repeat=rep,
                      remind=a.get("remind")) if ids else None
        if not item:
            speak("I couldn't find that on your agenda.")
            return "Agenda update: no match"
        speak(f"Updated: {item['text']}, {describe(item)}.")
        return f"Agenda: ~ {item['id']}"
    if cmd == "show":
        show()
        speak("Here's your agenda.")
        return "Agenda shown"
    answer = _speak_list(a.get("range") or "week")
    speak(answer)
    return "Agenda listed"
