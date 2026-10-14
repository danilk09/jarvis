"""
The daily breakdown: "Jarvis, give me a breakdown of today."

Gathers in parallel: weather (Open-Meteo, no key), headlines (Brave News), the agenda
(today and the next few days — never anything overdue) and special days (holidays, sky
events, launches — events.py). Also given for "good morning / afternoon / evening".
Unread email is checked at the same time by a background Claude Code task with the claude.ai
Gmail connector (delegate "google"); it takes longer, so it's spoken as a short follow-up right
after the breakdown (then any check-in question, see checkins.hold). One Haiku call
turns the rest into a spoken briefing that opens with whatever makes today special ("Merry Christmas, sir...") and
mentions sky events as an aside; the full breakdown goes on the Stage as a note.
"""

import concurrent.futures
import datetime as dt
import re
import threading

import requests

from . import agenda, brain, checkins, config, delegate, events, memory, stage, verbosity
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
                    model=config.MODEL, **config.NO_THINK, max_tokens=30,
                    system="From these facts about a user, return ONLY the city they live in now, as "
                           "'City, State' or 'City, Country' — or NONE if the facts don't say.",
                    messages=[{"role": "user", "content": "\n".join(places)}])
                city = config.reply_text(resp).strip().strip(".")
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
    from .stage_actions import geocode
    if city and not geocode(city):        # misheard ("follow me there"): don't remember it
        speak(f"I couldn't find a place called {city}. I'll ask again next time.")
        return ""
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


_INBOX = ("Check my unread email in Gmail from the last day (primary inbox, not promotions or social). "
          "Don't change anything. Say how many there are, then only the personal or time-sensitive ones — "
          "who and what, a few words each — in at most two spoken sentences starting \"In your inbox,\". "
          "If there's nothing unread, just say your inbox is clear.")


def check_email():
    """Start the inbox check in the background. Returns {"done": Event, "summary"}, or None."""
    from . import coding
    if not (config.BRIEFING_EMAIL and delegate.google_ready()) or coding.busy("jarvis-google"):
        return None
    box = {"done": threading.Event(), "summary": ""}

    def done(status, summary):
        box["summary"] = summary if status == "done" else ""
        box["done"].set()
    started = delegate.run("google", _INBOX, model="haiku", effort="low", quiet=True, on_done=done)
    return box if started.startswith("Claude Code working") else None


def _speak_email(box):
    """After the breakdown: the inbox summary, once the check finishes (up to two minutes)."""
    try:
        if box["done"].wait(120) and box["summary"]:
            from .coding import _speakable
            speak(_speakable(box["summary"]))
    finally:
        checkins.release()


def gather():
    """Everything for the breakdown, fetched in parallel. Missing sources are just left out."""
    today = dt.date.today()
    city = home_city()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
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
        today=[oi for oi in up if oi[0]["date"] == now.date()],
        coming=[oi for oi in up if oi[0]["date"] != now.date() and oi[1]["kind"] in ("deadline", "appointment")],
        todo=agenda.undated(),
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
    agenda_lines = [f"TODAY: {agenda._due_phrase(o, i, now)}" for o, i in g["today"]]
    agenda_lines += [f"COMING UP: {agenda._due_phrase(o, i, now)}" for o, i in g["coming"]]
    agenda_lines += [f"TO-DO (no date): {i['text']}" for i in g["todo"][:5]]
    parts.append("AGENDA:\n" + ("\n".join(agenda_lines) if agenda_lines else "nothing scheduled"))
    if g.get("news"):
        parts.append("HEADLINES:\n" + "\n".join(f"- {n['title']}" for n in g["news"][:6]))   # titles only: keeps it short
    return "\n\n".join(parts)


_SYSTEM = """You are JARVIS, a witty, warm British butler of an AI, giving the user their daily breakdown out loud.
Return ONLY JSON: {"speech": "<what you say>"}.
Keep it brisk: the user wants the gist, not a report. In this order:
- Greeting: a holiday ("Merry Christmas, sir. Here's the breakdown..."), the user's birthday or another personal occasion from MEMORY — otherwise just a short good morning/afternoon/evening. Never say there's nothing special today, and never open with a launch or sky event.
- Weather: one short sentence (now, high, sky). No tips or advice unless rain is likely (40%+) or it's very hot or cold; never mention small rain chances.
- Agenda: today's items with times, then every COMING UP deadline or appointment, briefly ("a discussion's due Friday"). Never mention anything overdue, missed or unfinished from earlier days; if it's empty, say the schedule is clear in a few words. A big one today (an exam, an interview): wish them luck, and if MEMORY has something they're working on for it, mention it in a clause ("overflow's worth one more look").
- News: the two most important headlines in ONE sentence, a few words each ("In the news: Six Flags shut nine rides after a fatal crash, and the Fed held rates."). No details, no explanations, no filler about minor or business stories.
- At most ONE extra (a holiday in the next few days, a sky event, a launch), in a short fun clause at the very end — or nothing. Pick the most interesting; skip routine satellite launches.
- Plain spoken sentences: no markdown, lists, URLs or emoji. Say "sir" once at most.
- LENGTH: """


def compose(g):
    length = verbosity.rule(short="4 to 6 short sentences in total", long="up to 12 sentences, with more detail")
    known = memory.prompt_block()
    resp = config.client.messages.create(
        model=config.MODEL, **config.NO_THINK, max_tokens=verbosity.tokens(400, 1000),
        system=_SYSTEM + length + (f"\n\n{known}" if known else ""),
        messages=[{"role": "user", "content": _facts_for_claude(g)}])
    brain.log_usage("briefing", resp.usage)
    raw = config.reply_text(resp).strip()
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
    rows = [f"- {('**' + ('due ' if i['kind'] == 'deadline' else '') + agenda._clock(o['time']) + '** ') if o['time'] else ''}{i['text']}"
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
        rules=['"give me a breakdown of today", "daily breakdown", "brief me", "morning briefing", "what does today look like", "catch me up on today", "good morning/afternoon/evening" → type "briefing" (weather, agenda, news and anything special today). Just "what do I have today" is agenda "list".'])
def _briefing(a, chain):
    # speak("Pulling together your breakdown.")
    inbox = check_email()      # slower than the rest: spoken after the breakdown
    g = gather()
    try:
        speech = compose(g)
    except Exception as e:
        print(f"  [briefing] compose failed: {e}")
        speech = agenda._speak_list("today")
    _show(f"Today — {g['now']:%A, %B %d}".replace(" 0", " "), markdown(g))
    speak(speech)
    if inbox:
        checkins.hold()      # a check-in question waits until after the inbox summary
        threading.Thread(target=_speak_email, args=(inbox,), daemon=True).start()
    brain.remember("[Daily breakdown]", speech)
    return "Briefing given"
