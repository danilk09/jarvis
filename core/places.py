"""
Places on a street map: "show me pizza restaurants in Honolulu."

1. The area is geocoded (Nominatim) to a bounding box.
2. A Brave web search for "best <what> in <where>" gives the candidates; Haiku picks the
   specific named places the results mention and writes a one-line description of each
   from them (nothing invented).
3. Each place is located on OpenStreetMap with Photon (in parallel, inside the area); any
   it can't find is dropped. Without a Brave key (or too few results) Photon's own search
   for the kind of place is used. Opening hours and websites are filled in afterwards
   from Overpass, which is too slow to wait for.

The result is a "places" panel: a numbered list of options beside a 2-D map. Clicking an
option — or Jarvis talking about it — flies the map there and points the beam at the pin.
"""

import concurrent.futures
import difflib
import re
import threading

import requests

from . import brain, config, stage, verbosity
from .registry import action
from .tts import speak

_UA = {"User-Agent": "JARVIS-personal-assistant/1.0"}
_MAX_SPAN = 0.25          # degrees: big areas (a whole county) are clipped to ~25 km around the center
NARRATE = 3               # options Jarvis reads aloud, pointing at each


def _area(where):
    from .stage_actions import geocode
    place = geocode(where)
    if not place:
        return None
    lat, lon = place["lat"], place["lon"]
    w, s, e, n = place.get("bbox") or [lon - 0.1, lat - 0.1, lon + 0.1, lat + 0.1]
    w, e = max(w, lon - _MAX_SPAN), min(e, lon + _MAX_SPAN)
    s, n = max(s, lat - _MAX_SPAN), min(n, lat + _MAX_SPAN)
    pad = 0.02
    return {"name": where, "lat": lat, "lon": lon, "bbox": [w - pad, s - pad, e + pad, n + pad]}


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("&", "and")).strip()


# ── Candidates from the web ───────────────────────────────────────────────────
_PICK_SYSTEM = """You pick places for JARVIS, a voice assistant, from web search results.
Return ONLY JSON: {"intro":"<one short spoken sentence introducing the options>","places":[{"name":"<the place's exact name>","address":"<street address or neighborhood if the results give one, else empty>","description":"<what it's known for, max 18 words, only from the results>"}]}
- Only specific, named places located in the requested area that match what the user wants (not articles, lists, chains' head offices or places elsewhere).
- Order by how strongly the results recommend them. No duplicates.
- Never invent details, ratings or prices that aren't in the results."""


def _web_candidates(what, where, count):
    if not config.BRAVE_API_KEY:
        return [], ""
    try:
        r = requests.get("https://api.search.brave.com/res/v1/web/search", timeout=8,
                         headers={"Accept": "application/json", "X-Subscription-Token": config.BRAVE_API_KEY},
                         params={"q": f"best {what} in {where}", "count": 10, "extra_snippets": "true"})
        r.raise_for_status()
        results = r.json().get("web", {}).get("results", [])
    except Exception as e:
        print(f"  [places] search failed: {e}")
        return [], ""
    text = "\n\n".join(f"{x.get('title', '')}\n{x.get('description', '')}\n" + "\n".join(x.get("extra_snippets") or [])
                       for x in results)
    text = re.sub(r"<[^>]+>", "", text)[:12000]
    try:
        resp = config.client.messages.create(
            model=config.MODEL, max_tokens=900, system=_PICK_SYSTEM,
            messages=[{"role": "user", "content": f"The user wants: {what} in {where}. Up to {count + 2} places.\n\n"
                                                  f"SEARCH RESULTS:\n{text}"}])
        brain.log_usage("places", resp.usage)
        data = brain.parse_json(resp.content[0].text.strip()) or {}
    except Exception as e:
        print(f"  [places] picking failed: {e}")
        return [], ""
    picked = [p for p in data.get("places", []) if isinstance(p, dict) and p.get("name")]
    return picked, data.get("intro", "")


# ── Locating them ─────────────────────────────────────────────────────────────
def _photon(q, area, limit=3):
    """OpenStreetMap search via Photon (fast, fine to call in parallel), limited to the area."""
    try:
        r = requests.get("https://photon.komoot.io/api/", headers=_UA, timeout=8, params={
            "q": q, "lat": area["lat"], "lon": area["lon"], "limit": limit,
            "bbox": ",".join(str(v) for v in area["bbox"])})
        r.raise_for_status()
        return r.json().get("features", [])
    except Exception as e:
        print(f"  [places] Photon failed for {q!r}: {e}")
        return []


def _from_feature(f, extra=None):
    pr = f.get("properties", {})
    lon, lat = f["geometry"]["coordinates"][:2]
    street = f"{pr.get('housenumber', '')} {pr.get('street', '')}".strip()
    return {"name": pr.get("name", ""), "lat": lat, "lon": lon, "address": street or pr.get("district", ""),
            "description": "", "website": "", "hours": "",
            "osm": f"{pr.get('osm_type', '')}{pr.get('osm_id', '')}", **(extra or {})}


def _match(c, area):
    """The OSM feature that is this candidate, or None."""
    want = _norm(c["name"])
    best, best_score = None, 0.0
    for f in _photon(c["name"], area):
        name = _norm(f.get("properties", {}).get("name", ""))
        score = difflib.SequenceMatcher(None, want, name).ratio() + (0.3 if want and (want in name or name in want) else 0)
        if score > best_score:
            best, best_score = f, score
    if not best or best_score < 0.75:
        return None
    return _from_feature(best, {k: v for k, v in c.items() if v and k in ("description", "address")} | {"name": c["name"]})


def _locate(candidates, area):
    """Give each candidate lat/lon, dropping the ones that can't be found in the area."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return [p for p in pool.map(lambda c: _match(c, area), candidates) if p]


def _osm_candidates(what, area, count):
    """No web results: OpenStreetMap's own search for the kind of place."""
    out, seen = [], set()
    for f in _photon(what, area, limit=count + 4):
        p = _from_feature(f)
        kind = f.get("properties", {}).get("osm_value", "").replace("_", " ")
        if p["name"] and _norm(p["name"]) not in seen:
            seen.add(_norm(p["name"]))
            out.append({**p, "description": kind.capitalize() if kind and kind != "yes" else ""})
    return out


def _enrich(pid, found):
    """Opening hours and websites from OpenStreetMap (Overpass is slow, so this runs after the
    panel is up and fills them in)."""
    ids = {"N": [], "W": [], "R": []}
    for p in found:
        if p.get("osm", "")[:1] in ids and p["osm"][1:].isdigit():
            ids[p["osm"][0]].append(p["osm"][1:])
    parts = "".join(f"{t}(id:{','.join(v)});" for t, v in (("node", ids["N"]), ("way", ids["W"]), ("relation", ids["R"])) if v)
    if not parts:
        return
    try:
        r = requests.post("https://overpass-api.de/api/interpreter", headers=_UA, timeout=30,
                          data={"data": f"[out:json][timeout:25];({parts});out tags;"})
        r.raise_for_status()
        tags = {f"{e['type'][0].upper()}{e['id']}": e.get("tags", {}) for e in r.json().get("elements", [])}
    except Exception as e:
        print(f"  [places] Overpass unavailable: {e}")
        return
    panel = stage.get(pid)
    if not panel:
        return
    places = [dict(p) for p in panel["data"].get("places", [])]
    for p in places:
        t = tags.get(p.get("osm", ""), {})
        p["hours"] = p.get("hours") or t.get("opening_hours", "")
        p["website"] = p.get("website") or t.get("website") or t.get("contact:website", "")
        if not p.get("address") and t.get("addr:street"):
            p["address"] = f"{t.get('addr:housenumber', '')} {t['addr:street']}".strip()
    stage.update(pid, places=places)


def find(what, where, count=6):
    """[{"name","lat","lon","address","description","website","hours","osm"}], intro, area —
    area is None if the place can't be found."""
    area = _area(where)
    if not area:
        return None, "", None
    candidates, intro = _web_candidates(what, where, count)
    found = _locate(candidates, area) if candidates else []
    if len(found) < 2:
        known = {_norm(f["name"]) for f in found}
        found += [p for p in _osm_candidates(what, area, count) if _norm(p["name"]) not in known]
    return found[:count], intro, area


# ── The Stage ─────────────────────────────────────────────────────────────────
def show(title, places, area, query):
    data = {"query": query, "places": places, "area": area, "active": -1}
    pid = next((p["id"] for p in stage.panels() if p["kind"] == "places"), None)
    if pid:
        flight = (stage.get(pid)["data"].get("flight", 0)) + 1
        stage.update(pid, title=title, flight=flight, **data)
        stage.navigate("/stage")
        return pid
    return stage.add("places", title, {**data, "flight": 1})


def point_at(pid, i):
    """Mark option i: the list highlights it, the map flies there, the beam points at the pin."""
    p = stage.get(pid)
    places = p["data"].get("places", []) if p else []
    if 0 <= i < len(places):
        stage.update(pid, active=i)
        stage.highlight(pid, {"place": i}, places[i]["name"])


@action("find_places",
        schema='{"type":"find_places","what":"<kind of place, e.g. pizza restaurants>","where":"<city or area; empty = where the user lives>","count":6}',
        rules=['"show me pizza restaurants in Honolulu", "find coffee shops near X", "where can I get sushi in Y", "good hiking trails around Z", "bars near me" → type "find_places" (a street map with a list of options to compare). "where is X", "show X on the map", "the globe" stay "show_map".'])
def _find_places(a, chain):
    what = (a.get("what") or "").strip()
    where = (a.get("where") or "").strip()
    if not where:
        from .briefing import home_city
        where = home_city(ask=False)
    if not what:
        speak("What kind of place are you looking for?")
        return "Places: no query"
    if not where:
        speak(f"Where should I look for {what}?")
        return "Places: no area"
    count = max(2, min(10, int(a.get("count") or 6))) if str(a.get("count") or "6").isdigit() else 6
    speak(f"Looking for {what} in {where}.")
    places, intro, area = find(what, where, count)
    if area is None:
        speak(f"I couldn't find {where} on the map.")
        return "Places: unknown area"
    if not places:
        speak(f"I couldn't find any {what} in {where}.")
        return "Places: none found"
    pid = show(f"{what[0].upper() + what[1:]} — {where}", places, area, what)
    threading.Thread(target=_enrich, args=(pid, places), daemon=True).start()
    brain.remember(f"[Showed {what} in {where} on a map]",
                   "; ".join(f"{i + 1}. {p['name']}: {p.get('description', '')}" for i, p in enumerate(places)))
    speak(intro or f"Here are {len(places)} options.")
    for i, p in enumerate(places[:verbosity.tokens(NARRATE, len(places))]):
        line = f"{p['name']}" + (f": {p['description'].rstrip('.')}." if p.get("description") else ".")
        speak(line, on_start=lambda i=i: point_at(pid, i))
    if len(places) > NARRATE and not verbosity.detailed:
        speak("The rest are on the stage.")
    return f"Places: {len(places)} {what} in {where}"
