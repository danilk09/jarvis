"""
The daily breakdown: "Jarvis, give me a breakdown of today."

Gathers in parallel: weather (Open-Meteo, no key), headlines (Brave News), the agenda
(overdue, today, the next few days), special days (holidays, sky events, launches —
events.py) and yesterday's summary from memory. One Haiku call turns it into a spoken
briefing that opens with whatever makes today special ("Merry Christmas, sir...") and
mentions sky events as an aside; the full breakdown goes on the Stage as a note.
"""

import concurrent.futures
import datetime as dt
import os
import re

import requests

from . import agenda, brain, config, events, memory, stage, verbosity
from .registry import action
from .tts import speak, wait_tts

_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
        81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
        95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}

_city = {"key": None, "city": ""}


# ── Where the user lives ──────────────────────────────────────────────────────
def home_city(ask=True):
    """config.HOME_CITY, else the city in memory, else ask once and remember the answer."""
    if config.HOME_CITY:
        return config.HOME_CITY
    facts = memory.facts()
    key = tuple(f["text"] for f in facts)
    if _city["key"] != key:
        _city.update(key=key, city="")
        places = [f["text"] for f in facts if f["section"] in ("about", "places")]
        if places:
            try:
                resp = config.client.messages.create(
                    model=config.MODEL, max_tokens=30,
                    system="From these facts about a user, return ONLY the city they live in now, as "
                           "'City, State' or 'City, Country' — or NONE if the facts don't say.",
                    messages=[{"role": "user", "content": "\n".join(places)}])
                city = resp.content[0].text.strip().strip(".")
                _city["city"] = "" if city.upper().startswith("NONE") else city
            except Exception as e:
                print(f"  [briefing] city lookup failed: {e}")
    if _city["city"] or not ask:
        return _city["city"]
    from . import audio
    speak("Which city should I use for your weather?")
    wait_tts()
    answer = audio.listen_for_command(max_duration=6, silence_duration=0.8)
    city = re.sub(r"^(i live in|i'm in|i am in|it's|in|use)\s+", "", (answer or "").strip().rstrip(".!?"),
                  flags=re.IGNORECASE).strip()
    if city:
        memory.add(f"The user lives in {city}.", "places")
        _city.update(key=None, city=city)
    return city


# ── Sources ───────────────────────────────────────────────────────────────────
def weather(city):
    from .stage_actions import geocode
    place = geocode(city) if city else None
    if not place:
        return None
    imperial = config.UNITS == "imperial"
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=8, params={
            "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunset",
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [briefing] weather unavailable: {e}")
        return None
    cur, day = data.get("current", {}), data.get("daily", {})
    first = lambda k: (day.get(k) or [None])[0]
    sunset = first("sunset")
    return {
        "city": city, "unit": "°F" if imperial else "°C", "wind_unit": "mph" if imperial else "km/h",
        "now": round(cur.get("temperature_2m", 0)), "feels": round(cur.get("apparent_temperature", 0)),
        "now_sky": _WMO.get(cur.get("weather_code"), ""), "wind": round(cur.get("wind_speed_10m", 0)),
        "high": round(first("temperature_2m_max") or 0), "low": round(first("temperature_2m_min") or 0),
        "sky": _WMO.get(first("weather_code"), ""), "rain": first("precipitation_probability_max"),
        "sunset": dt.datetime.fromisoformat(sunset).strftime("%I:%M %p").lstrip("0") if sunset else "",
    }


def headlines(count=5):
    if not config.BRAVE_API_KEY:
        return []
    out, seen = [], set()
    for query in config.BRIEFING_NEWS:
        try:
            r = requests.get("https://api.search.brave.com/res/v1/news/search", timeout=8,
                             headers={"Accept": "application/json", "X-Subscription-Token": config.BRAVE_API_KEY},
                             params={"q": query, "count": count, "freshness": "pd"})
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:
            print(f"  [briefing] news unavailable: {e}")
            continue
        for n in results:
            title = re.sub(r"<[^>]+>", "", n.get("title", "")).strip()
            title = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,40}$", "", title)        # " - The New York Times"
            if len(title) < 50 and re.search(r"\b(top stories|headlines|news roundup)\b", title, re.IGNORECASE):
                continue
            if title and title.lower() not in seen:
                seen.add(title.lower())
                out.append({"title": title, "url": n.get("url", ""),
                            "source": (n.get("meta_url") or {}).get("hostname", "").removeprefix("www."),
                            "description": re.sub(r"<[^>]+>", "", n.get("description", ""))[:240]})
    return out[:count + 2]


def _yesterday():
    path = os.path.join(memory._DAILY_DIR, f"{dt.date.today() - dt.timedelta(days=1)}.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def gather():
    """Everything for the breakdown, fetched in parallel. Missing sources are just left out."""
    today = dt.date.today()
    city = home_city()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {"weather": pool.submit(weather, city), "news": pool.submit(headlines),
                "special": pool.submit(events.on, today), "launches": pool.submit(events.launches, today),
                "soon": pool.submit(lambda: [(d, n) for d, n in events.upcoming(6) if d > today])}
        got = {}
        for name, job in jobs.items():
            try:
                got[name] = job.result(timeout=15)
            except Exception as e:
                print(f"  [briefing] {name} failed: {e}")
                got[name] = None
    now = dt.datetime.now()
    up = agenda.upcoming(3, now)
    got.update(
        now=now,
        overdue=agenda.overdue(now),
        today=[oi for oi in up if oi[0]["date"] == now.date()],
        coming=[oi for oi in up if oi[0]["date"] != now.date() and oi[1]["kind"] in ("deadline", "appointment")],
        todo=agenda.undated(),
        yesterday=_yesterday(),
    )
    return got


# ── Writing it up ─────────────────────────────────────────────────────────────
def _facts_for_claude(g):
    """The gathered data as plain text for the composing call."""
    now = g["now"]
    parts = [f"NOW: {now:%A, %B %d %Y, %I:%M %p}"]
    w = g.get("weather")
    if w:
        parts.append(f"WEATHER in {w['city']}: now {w['now']}{w['unit']} (feels {w['feels']}), {w['now_sky']}, "
                     f"wind {w['wind']} {w['wind_unit']}; today {w['sky']}, high {w['high']}, low {w['low']}, "
                     f"{w['rain'] or 0}% chance of rain; sunset {w['sunset']}")
    special = g.get("special") or []
    if special:
        parts.append("SPECIAL TODAY: " + "; ".join(f"{s['name']} ({s['kind']}){': ' + s['note'] if s['note'] else ''}"
                                                   for s in special))
    if g.get("launches"):
        parts.append("ROCKET LAUNCHES TODAY: " + "; ".join(f"{l['name']} at {l['time']} from {l['where']}"
                                                          for l in g["launches"]))
    if g.get("soon"):
        parts.append("HOLIDAYS IN THE NEXT FEW DAYS: " + "; ".join(f"{n} ({d:%A})" for d, n in g["soon"]))
    agenda_lines = [f"OVERDUE: {agenda._due_phrase(o, i, now)}" for o, i in g["overdue"]]
    agenda_lines += [f"TODAY: {agenda._due_phrase(o, i, now)}" for o, i in g["today"]]
    agenda_lines += [f"COMING UP: {agenda._due_phrase(o, i, now)}" for o, i in g["coming"]]
    agenda_lines += [f"TO-DO (no date): {i['text']}" for i in g["todo"][:5]]
    parts.append("AGENDA:\n" + ("\n".join(agenda_lines) if agenda_lines else "nothing scheduled"))
    if g.get("news"):
        parts.append("HEADLINES:\n" + "\n".join(f"- {n['title']} ({n['source']}): {n['description']}"
                                                for n in g["news"][:6]))
    if g.get("yesterday"):
        parts.append(f"YESTERDAY (summary of the user's conversations with you): {g['yesterday']}")
    return "\n\n".join(parts)


_SYSTEM = """You are JARVIS, a witty, warm British butler of an AI, giving the user their daily breakdown out loud.
Return ONLY JSON: {"speech": "<what you say>"}.
- Open with what makes today special, if anything, as a natural greeting: a holiday ("Merry Christmas, sir. Here's the breakdown..."), the user's birthday or another personal occasion from MEMORY. Otherwise just a short good morning/afternoon/evening that fits the time.
- Then the weather in one sentence, with a practical tip only if it matters (umbrella, jacket).
- Then the agenda: anything overdue first, gently; then today's items with times; then deadlines in the next couple of days. If it's empty, say the schedule is clear.
- Then two or three headlines, one short clause each.
- Sky events, launches and upcoming holidays go near the end as a light aside, e.g. "If you're interested, the Orionid meteor shower peaks tonight." Make them sound fun, not like a list.
- Mention yesterday only if something was left unfinished.
- Plain spoken sentences: no markdown, lists, URLs or emoji. Say "sir" once or twice at most.
- LENGTH: """


def compose(g):
    length = verbosity.rule(short="about 7 to 9 sentences, brisk", long="up to 14 sentences, with more detail")
    known = memory.prompt_block()
    resp = config.client.messages.create(
        model=config.MODEL, max_tokens=verbosity.tokens(700, 1100),
        system=_SYSTEM + length + (f"\n\n{known}" if known else ""),
        messages=[{"role": "user", "content": _facts_for_claude(g)}])
    brain.log_usage("briefing", resp.usage)
    raw = resp.content[0].text.strip()
    data = brain.parse_json(raw)
    return (data or {}).get("speech") or raw


def markdown(g):
    now = g["now"]
    out = []
    special = [s for s in (g.get("special") or []) if s["kind"] in ("holiday", "clock")]
    if special:
        out.append("**" + " · ".join(s["name"] for s in special) + "**  ")
        out += [f"_{s['note']}_" for s in special if s["note"]]
        out.append("")
    w = g.get("weather")
    if w:
        rain = f", {w['rain']}% chance of rain" if w.get("rain") else ""
        out += [f"### Weather — {w['city']}",
                f"**{w['now']}{w['unit']}** now, {w['now_sky']} (feels like {w['feels']}{w['unit']})  ",
                f"High {w['high']}{w['unit']} / low {w['low']}{w['unit']}, {w['sky']}{rain}. Sunset {w['sunset']}.", ""]
    rows = [f"- **Overdue:** {i['text']} — was due {agenda.when_words(o, now)}" for o, i in g["overdue"]]
    rows += [f"- {('**' + ('due ' if i['kind'] == 'deadline' else '') + agenda._clock(o['time']) + '** ') if o['time'] else ''}{i['text']}"
             f"{' _(' + i['group'] + ')_' if i.get('group') else ''}" for o, i in g["today"]]
    out += ["### Today", *(rows or ["- Nothing scheduled"]), ""]
    if g["coming"]:
        out += ["### Coming up", *[f"- {agenda.when_words(o, now)} — {i['text']}" for o, i in g["coming"]], ""]
    if g["todo"]:
        out += ["### To-do", *[f"- {i['text']}" for i in g["todo"][:8]], ""]
    if g.get("news"):
        out += ["### Headlines", *[f"- [{n['title']}]({n['url']})" + (f" — {n['source']}" if n["source"] else "")
                                   for n in g["news"][:6]], ""]
    sky = [s["note"][0].upper() + s["note"][1:] for s in (g.get("special") or []) if s["kind"] == "sky"]
    sky += [f"Launch: {l['name']}, {l['time']} from {l['where']}" for l in (g.get("launches") or [])]
    sky += [f"{n} on {d:%A}" for d, n in (g.get("soon") or [])]
    if sky:
        out += ["### Also today", *[f"- {s}" for s in sky], ""]
    return "\n".join(out).strip()


def _show(title, md):
    pid = next((p["id"] for p in stage.panels() if p["kind"] == "note" and stage.private(p["id"]).get("briefing")), None)
    if pid:
        stage.update(pid, title=title, markdown=md)
        stage.navigate("/stage")
    else:
        stage.add("note", title, {"markdown": md}, private={"briefing": True})


@action("briefing",
        schema='{"type":"briefing"}',
        rules=['"give me a breakdown of today", "daily breakdown", "brief me", "morning briefing", "what does today look like", "catch me up on today" → type "briefing" (weather, agenda, news and anything special today). Just "what do I have today" is agenda "list".'])
def _briefing(a, chain):
    speak("Pulling together your breakdown.")
    g = gather()
    try:
        speech = compose(g)
    except Exception as e:
        print(f"  [briefing] compose failed: {e}")
        speech = agenda._speak_list("today")
    _show(f"Today — {g['now']:%A, %B %d}".replace(" 0", " "), markdown(g))
    speak(speech)
    brain.remember("[Daily breakdown]", speech)
    return "Briefing given"
