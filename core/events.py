"""
Special days for the daily breakdown: holidays and fun observances (US), daylight
saving changes, moon phases and seasons (US Naval Observatory API, with a fallback),
meteor shower peaks, eclipses and today's rocket launches (Launch Library 2).

    on(date)            -> [{"name", "kind", "note"}]   everything special on that date
    upcoming(days=60)   -> [(date, name)]               holidays ahead (for the agenda prompt)
    launches(date)      -> [{"name", "time", "where"}]
"""

import datetime as dt
import threading

import requests

_TIMEOUT = 6
_cache: dict = {}
_lock = threading.Lock()

# Fixed-date holidays and observances: (month, day) -> name
_FIXED = {
    (1, 1): "New Year's Day", (2, 2): "Groundhog Day", (2, 14): "Valentine's Day", (3, 14): "Pi Day",
    (3, 17): "St. Patrick's Day", (4, 1): "April Fools' Day", (4, 22): "Earth Day", (5, 4): "Star Wars Day",
    (5, 5): "Cinco de Mayo", (6, 19): "Juneteenth", (7, 4): "Independence Day", (10, 31): "Halloween",
    (11, 11): "Veterans Day", (12, 24): "Christmas Eve", (12, 25): "Christmas Day", (12, 31): "New Year's Eve",
}

# Meteor shower peak nights (approximate — they shift by a day year to year): (month, day) -> (name, rate note)
_METEORS = {
    (1, 3): ("Quadrantid meteor shower", "up to 100 an hour before dawn"),
    (4, 22): ("Lyrid meteor shower", "about 20 an hour after midnight"),
    (5, 5): ("Eta Aquariid meteor shower", "best in the hours before dawn"),
    (7, 30): ("Southern Delta Aquariid meteor shower", "about 20 an hour after midnight"),
    (8, 12): ("Perseid meteor shower", "up to 100 an hour, one of the best of the year"),
    (10, 8): ("Draconid meteor shower", "best in the early evening"),
    (10, 21): ("Orionid meteor shower", "about 20 an hour after midnight"),
    (11, 17): ("Leonid meteor shower", "about 15 an hour after midnight"),
    (12, 14): ("Geminid meteor shower", "up to 120 an hour, the best of the year"),
    (12, 22): ("Ursid meteor shower", "about 10 an hour"),
}

# Eclipses: date -> (description, where it's visible)
_ECLIPSES = {
    dt.date(2026, 2, 17): ("annular solar eclipse", "Antarctica"),
    dt.date(2026, 3, 3): ("total lunar eclipse", "the Americas, East Asia and Australia"),
    dt.date(2026, 8, 12): ("total solar eclipse", "Greenland, Iceland and Spain"),
    dt.date(2026, 8, 28): ("partial lunar eclipse", "the Americas, Europe and Africa"),
    dt.date(2027, 2, 6): ("annular solar eclipse", "South America and West Africa"),
    dt.date(2027, 2, 20): ("penumbral lunar eclipse", "the Americas, Europe, Africa and Asia"),
    dt.date(2027, 7, 18): ("penumbral lunar eclipse", "Asia, Australia and the Pacific"),
    dt.date(2027, 8, 2): ("total solar eclipse", "Spain, North Africa and the Middle East"),
    dt.date(2027, 8, 17): ("penumbral lunar eclipse", "the Americas and the Pacific"),
    dt.date(2028, 1, 12): ("partial lunar eclipse", "the Americas, Europe and Africa"),
    dt.date(2028, 1, 26): ("annular solar eclipse", "South America and Spain"),
    dt.date(2028, 7, 6): ("partial lunar eclipse", "Asia, Australia and Africa"),
    dt.date(2028, 7, 22): ("total solar eclipse", "Australia and New Zealand"),
    dt.date(2028, 12, 31): ("total lunar eclipse", "Europe, Africa, Asia and Australia"),
}


def _nth_weekday(year, month, weekday, n):
    """n-th weekday (0 = Monday) of a month; n = -1 for the last one."""
    if n > 0:
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return dt.date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def holidays(year):
    """{date: name} for the year, US holidays plus fun observances."""
    out = {dt.date(year, m, d): name for (m, d), name in _FIXED.items()}
    floating = {
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Presidents' Day",
        _nth_weekday(year, 5, 6, 2): "Mother's Day",
        _nth_weekday(year, 5, 0, -1): "Memorial Day",
        _nth_weekday(year, 6, 6, 3): "Father's Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 10, 0, 2): "Columbus Day and Indigenous Peoples' Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving",
        _easter(year): "Easter Sunday",
    }
    out.update(floating)
    out[_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1)] = "Black Friday"
    return out


def _dst(year):
    """US daylight saving: clocks go forward the 2nd Sunday of March, back the 1st Sunday of November."""
    return {_nth_weekday(year, 3, 6, 2): "Daylight saving time starts tonight at 2 AM: clocks spring forward an hour",
            _nth_weekday(year, 11, 6, 1): "Daylight saving time ends tonight at 2 AM: clocks fall back an hour"}


def _fetch_json(url, params=None):
    key = (url, tuple(sorted((params or {}).items())))
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        r = requests.get(url, params=params, timeout=_TIMEOUT,
                         headers={"User-Agent": "JARVIS-personal-assistant/1.0"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [events] {url.split('/')[2]} unavailable: {e}")
        return None
    with _lock:
        _cache[key] = data
    return data


def _moon_phases(d):
    """{date: phase name} for the major phases around `d`. USNO, falling back to the mean lunar cycle."""
    data = _fetch_json("https://aa.usno.navy.mil/api/moon/phases/date",
                       {"date": (d - dt.timedelta(days=3)).isoformat(), "nump": 6})
    out = {}
    for p in (data or {}).get("phasedata", []):
        try:   # times are UT: convert, or a late-evening full moon lands on the wrong day
            h, m = (int(x) for x in p.get("time", "12:00").split(":"))
            ut = dt.datetime(p["year"], p["month"], p["day"], h, m, tzinfo=dt.timezone.utc)
            out[ut.astimezone().date()] = p["phase"]
        except (KeyError, TypeError, ValueError):
            continue
    if out:
        return out
    # Fallback: mean synodic month from a known new moon (accurate to about a day)
    synodic, ref = 29.530588853, dt.datetime(2000, 1, 6, 18, 14)
    start = (dt.datetime.combine(d, dt.time()) - ref).total_seconds() / 86400 / synodic
    for k in range(int(start) - 1, int(start) + 2):
        for frac, name in ((0, "New Moon"), (0.25, "First Quarter"), (0.5, "Full Moon"), (0.75, "Last Quarter")):
            when = ref + dt.timedelta(days=(k + frac) * synodic)
            out[when.date()] = name
    return out


def _seasons(year):
    data = _fetch_json("https://aa.usno.navy.mil/api/seasons", {"year": year})
    out = {}
    for s in (data or {}).get("data", []):
        if s.get("phenom") in ("Equinox", "Solstice"):
            try:
                h, m = (int(x) for x in s.get("time", "12:00").split(":"))
                d = dt.datetime(s["year"], s["month"], s["day"], h, m, tzinfo=dt.timezone.utc).astimezone().date()
            except (KeyError, TypeError, ValueError):
                continue
            season = {3: "spring", 6: "summer", 9: "fall", 12: "winter"}.get(d.month, "")
            out[d] = f"{s['phenom'].lower()} — the first day of {season}" if season else s["phenom"].lower()
    return out


def on(d=None):
    """Everything special on date `d` (default today)."""
    d = d or dt.date.today()
    out = []
    if d in holidays(d.year):
        out.append({"name": holidays(d.year)[d], "kind": "holiday", "note": ""})
    if d in _dst(d.year):
        out.append({"name": "Daylight saving change", "kind": "clock", "note": _dst(d.year)[d]})
    if d in _seasons(d.year):
        out.append({"name": "Season change", "kind": "sky", "note": "The " + _seasons(d.year)[d]})
    phase = _moon_phases(d).get(d)
    if phase in ("Full Moon", "New Moon"):
        note = ("a full moon tonight" if phase == "Full Moon"
                else "a new moon — dark skies, good for stargazing")
        out.append({"name": phase, "kind": "sky", "note": note})
    if (d.month, d.day) in _METEORS:
        name, rate = _METEORS[(d.month, d.day)]
        out.append({"name": name, "kind": "sky", "note": f"the {name} peaks tonight, {rate}"})
    if d in _ECLIPSES:
        what, where = _ECLIPSES[d]
        out.append({"name": what.capitalize(), "kind": "sky", "note": f"a {what} today, visible from {where}"})
    return out


def upcoming(days=60, start=None):
    """Holidays in the next `days` days: [(date, name)]."""
    start = start or dt.date.today()
    end = start + dt.timedelta(days=days)
    out = []
    for year in {start.year, end.year}:
        out += [(d, n) for d, n in holidays(year).items() if start <= d <= end]
    return sorted(out)


def launches(d=None):
    """Rocket launches scheduled for date `d` (local time): [{"name", "time", "where"}]."""
    d = d or dt.date.today()
    lo = dt.datetime.combine(d, dt.time()).astimezone(dt.timezone.utc)
    hi = lo + dt.timedelta(days=1)
    data = _fetch_json("https://ll.thespacedevs.com/2.2.0/launch/upcoming/",
                       {"net__gte": lo.strftime("%Y-%m-%dT%H:%M:%SZ"), "net__lt": hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "limit": 6, "mode": "list"})
    out = []
    for r in (data or {}).get("results", []):
        try:
            net = dt.datetime.fromisoformat(r["net"].replace("Z", "+00:00")).astimezone()
        except (KeyError, ValueError):
            continue
        where = r.get("location") or ((r.get("pad") or {}).get("location") or {}).get("name", "")
        out.append({"name": r.get("name", "A launch"), "time": net.strftime("%I:%M %p").lstrip("0"), "where": where})
    return out
