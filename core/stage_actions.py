"""
Stage actions: what Jarvis can put on the Stage, how it reads and summarizes live
pages, edits files, finds places on the globe, rearranges panels by voice, and
points things out (highlights synced with what it is saying).
"""

import difflib
import os
import re
import threading
import time

import requests

from . import brain, config, media, stage
from .registry import action
from .tts import speak
from .web import brave_search

NARRATE_POINTS = 3      # key points Jarvis reads aloud (with highlights) after a summary
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"}


def _ask_json(system, content, max_tokens=800):
    resp = config.client.messages.create(model=config.MODEL, max_tokens=max_tokens, system=system,
                                         messages=[{"role": "user", "content": content}])
    return brain.parse_json(resp.content[0].text.strip()) or {}


# ── Text matching ─────────────────────────────────────────────────────────────
_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", "\xa0": " "})


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").translate(_PUNCT)).strip().lower()


def _verify_quote(quote, text):
    """Return the quote as it appears in `text`, the closest sentence if the model
    paraphrased slightly, or "" if nothing is close enough to highlight."""
    if not quote:
        return ""
    if _norm(quote) in _norm(text):
        return quote
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    best = max(sentences, key=lambda s: difflib.SequenceMatcher(None, _norm(s), _norm(quote)).ratio(),
               default="")
    if best and difflib.SequenceMatcher(None, _norm(best), _norm(quote)).ratio() > 0.72:
        return best.strip()
    return ""


# ── Pages ─────────────────────────────────────────────────────────────────────
def _fetch_article(url):
    """Download a page and pull out its main text. Returns {"title","paragraphs"} or None."""
    try:
        r = requests.get(url, headers=_UA, timeout=12)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"  Page fetch failed: {e}")
        return None
    title, text = "", ""
    try:
        import trafilatura
        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True) or ""
        meta = trafilatura.extract_metadata(html, default_url=url)
        title = (meta.title if meta else "") or ""
    except ImportError:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = m.group(1).strip() if m else ""
        body = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = "\n".join(re.sub(r"<[^>]+>", " ", p) for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S | re.I))
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in text.split("\n")]
    return {"title": title, "paragraphs": [p for p in paragraphs if len(p) > 1]}


def page_text(pid):
    """The readable text of a page panel: fetched server-side, or read from the live page in
    the desktop app when the site renders with JavaScript or blocks plain requests."""
    priv = stage.private(pid)
    if priv.get("paragraphs"):
        return priv["paragraphs"]
    url = (stage.get(pid) or {}).get("data", {}).get("url", "")
    art = _fetch_article(url) if url else None
    paragraphs = art["paragraphs"] if art else []
    if sum(len(p) for p in paragraphs) < 600:
        live = stage.request_extract(pid)
        if live and sum(len(p) for p in live.get("paragraphs", [])) > sum(len(p) for p in paragraphs):
            paragraphs = live["paragraphs"]
            art = {"title": live.get("title", ""), "paragraphs": paragraphs}
    if paragraphs:
        priv["paragraphs"] = paragraphs
        title = (art or {}).get("title")
        stage.update(pid, title=title or None, reader=paragraphs[:300])
    return paragraphs


def _search_url(query):
    for r in brave_search(query, count=5):
        if r.get("url", "").startswith("http"):
            return r["url"], r.get("title", "")
    return "", ""


def open_page(url, title=None):
    pid = stage.add("page", title or url, {"url": url})
    threading.Thread(target=page_text, args=(pid,), daemon=True).start()   # reader view + cache
    return pid


def _page_from_action(a, speak_first=True):
    """Find or open the page an action refers to. Returns the panel id or None."""
    url, query = (a.get("url") or "").strip(), (a.get("query") or "").strip()
    if not url and not query:
        pid = stage.resolve(a.get("panel") or "page")
        if pid and stage.get(pid)["kind"] == "page":
            return pid
        speak("Which page? Give me a topic or a site.")
        return None
    title = None
    if not url:
        if not config.BRAVE_API_KEY:
            speak("I need a Brave API key to search for pages. Add BRAVE_API_KEY to your .env file.")
            return None
        url, title = _search_url(query)
        if not url:
            speak("I couldn't find a page for that.")
            return None
    elif not url.startswith("http"):
        url = "https://" + url
    if speak_first:
        speak("Pulling it up.")
    return open_page(url, title)


@action("show_page",
        schema='{"type":"show_page","url":"<full url, or empty>","query":"<what to search for if no url>"}',
        rules=['"show me [site/page]", "pull up [page] on the stage", "display [website]" → type "show_page" (a live page on the Stage). Plain "open [website]" stays type "url" (external browser) unless they mention the stage or screen.'])
def _show_page(a, chain):
    pid = _page_from_action(a)
    return f"Page on stage: {pid}" if pid else "Show page: nothing opened"


_SUMMARY_SYSTEM = (
    "You summarize web articles for JARVIS, a voice assistant that shows the article on screen "
    "next to your key points. Return ONLY JSON: "
    '{"title":"<short title>","speech":"<2 spoken sentences: what it is about and the main takeaway. No markdown.>",'
    '"points":[{"text":"<one key point, one sentence, max 22 words>","quote":"<8-30 words copied EXACTLY, '
    'character for character, from ONE sentence of the article that supports the point>"}]} '
    "Give 3-6 points in the order they appear in the article. If the user said what they care about, focus on that."
)


@action("read_article",
        schema='{"type":"read_article","url":"<url, or empty>","query":"<topic to find an article about, or empty to use the page on the stage>","focus":"<what the user wants from it, or empty>"}',
        rules=['"summarize this article/page", "find an article about X and summarize it", "read me the news about X", "what are the key points of X" → type "read_article". Shows the live page plus a key-points panel and reads the summary aloud. Leave url and query empty to summarize the page already on the stage.'])
def _read_article(a, chain):
    pid = _page_from_action(a)
    if not pid:
        return "Read article: no page"
    speak("Reading it now.")
    paragraphs = page_text(pid)
    if not paragraphs:
        speak("I couldn't read that page. It may block automated access.")
        return "Read article: no text"
    text = "\n".join(paragraphs)
    page = stage.get(pid)
    try:
        summary = _ask_json(_SUMMARY_SYSTEM, f"User's interest: {a.get('focus') or 'general summary'}\n\n"
                                             f"Title: {page['title']}\n\nArticle:\n{text[:14000]}", 1200)
    except Exception as e:
        speak("Summarizing failed.")
        return f"Read article: {e}"
    points = [{"text": p.get("text", ""), "quote": _verify_quote(p.get("quote", ""), text)}
              for p in summary.get("points", []) if p.get("text")]
    sid = stage.add("summary", f"Key points — {summary.get('title') or page['title']}"[:90],
                    {"source": pid, "overview": summary.get("speech", ""), "points": points, "active": None})
    brain.remember(f"[Summarized the article on the stage: {page['title']} ({page['data'].get('url')})]",
                   summary.get("speech", "") + " Key points: " + " | ".join(p["text"] for p in points))
    narrate(pid, sid, summary.get("speech", ""), points)
    return f"Summarized {page['title']}"


def narrate(pid, sid, overview, points):
    """Speak the overview, then walk through the top points, highlighting each as it is spoken."""
    if overview:
        speak(overview)
    for i, p in enumerate(points[:NARRATE_POINTS]):
        def mark(i=i, p=p):
            stage.update(sid, active=i)
            if p["quote"]:
                stage.highlight(pid, {"quote": p["quote"]}, label=f"Point {i + 1}")
        speak(p["text"], on_start=mark)


# ── Pointing things out ───────────────────────────────────────────────────────
_POINT_TEXT_SYSTEM = (
    "You point things out in a document shown on screen. Return ONLY JSON: "
    '{"quote":"<5-30 words copied EXACTLY from the text that best answers the request>",'
    '"label":"<2-4 word label>","speech":"<one short spoken sentence about it>"} '
    'or {"quote":"","speech":"<say it is not in there>"} if it is not in the text.'
)
_POINT_FILE_SYSTEM = (
    "You point things out in a file shown in an editor. Lines are numbered. Return ONLY JSON: "
    '{"lines":[<first line>,<last line>],"label":"<2-4 word label>","speech":"<one short spoken sentence>"} '
    'or {"lines":[],"speech":"<say it is not in there>"}.'
)
_POINT_IMAGE_SYSTEM = (
    "You point things out in an image. Return ONLY JSON: "
    '{"region":[x,y,width,height],"label":"<2-4 word label>","speech":"<one short spoken sentence>"} '
    "with the region as fractions (0-1) of the image width and height, tight around the thing, "
    'or {"region":[],"speech":"<say you cannot see it>"}.'
)


def _point_in_text(pid, text, what, mark_pid=None):
    res = _ask_json(_POINT_TEXT_SYSTEM, f"Request: {what}\n\nText:\n{text[:14000]}", 300)
    quote = _verify_quote(res.get("quote", ""), text)
    if not quote:
        speak(res.get("speech") or "I couldn't find that in there.")
        return "Point out: not found"
    speak(res.get("speech") or "Here.",
          on_start=lambda: stage.highlight(mark_pid or pid, {"quote": quote}, res.get("label", "")))
    return f"Pointed out: {quote[:60]}"


@action("point_out",
        schema='{"type":"point_out","panel":"<panel number, kind or title — empty for the current one>","target":"<what to point at>"}',
        rules=['"show me where it says X", "point out X", "highlight X", "where is X", "which part/line ..." about something on the Stage → type "point_out"'])
def _point_out(a, chain):
    pid = stage.resolve(a.get("panel"))
    what = a.get("target", "")
    if not pid:
        speak("There's nothing on the stage to point at.")
        return "Point out: empty stage"
    p = stage.get(pid)
    kind = p["kind"]
    try:
        if kind == "summary":                 # point into the article the summary came from
            src = p["data"].get("source")
            if src and stage.get(src):
                return _point_in_text(src, "\n".join(page_text(src)), what)
            text = "\n".join([p["data"].get("overview", "")] + [pt["text"] for pt in p["data"].get("points", [])])
            return _point_in_text(pid, text, what)
        if kind == "page":
            return _point_in_text(pid, "\n".join(page_text(pid)), what)
        if kind == "note":
            return _point_in_text(pid, p["data"].get("markdown", ""), what)
        if kind == "file":
            lines = p["data"].get("content", "").splitlines()[:800]
            numbered = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))
            res = _ask_json(_POINT_FILE_SYSTEM, f"Request: {what}\n\nFile {p['title']}:\n{numbered}", 300)
            rng = [int(n) for n in res.get("lines", [])[:2] if str(n).isdigit()]
            if not rng:
                speak(res.get("speech") or "I couldn't find that in the file.")
                return "Point out: not found"
            rng = [max(1, min(rng)), min(len(lines), max(rng))]
            speak(res.get("speech") or "Here.",
                  on_start=lambda: stage.highlight(pid, {"lines": rng}, res.get("label", "")))
            return f"Pointed out lines {rng}"
        if kind == "image":
            path = stage.private(pid).get("path")
            b64, media_type = media.image_to_base64(path)
            resp = config.client.messages.create(
                model=config.MODEL, max_tokens=300, system=_POINT_IMAGE_SYSTEM,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": f"Point out: {what}"}]}])
            res = brain.parse_json(resp.content[0].text.strip()) or {}
            region = [float(v) for v in res.get("region", [])[:4]]
            if len(region) != 4:
                speak(res.get("speech") or "I can't see that in the image.")
                return "Point out: not found"
            speak(res.get("speech") or "Here.",
                  on_start=lambda: stage.highlight(pid, {"region": region}, res.get("label", "")))
            return f"Pointed out region {region}"
        if kind == "map":
            places = list(p["data"].get("places", []))
            names = [_norm(pl["name"]) for pl in places]
            hit = difflib.get_close_matches(_norm(what), names, n=1, cutoff=0.5)
            if hit:
                idx = names.index(hit[0])
            else:
                place = geocode(what)
                if not place:
                    speak(f"I couldn't find {what} on the map.")
                    return "Point out: place not found"
                places.append(place)
                idx = len(places) - 1
                stage.show_places(places, p["data"].get("route", False))
            flight = (stage.get(pid)["data"].get("flight", 0)) + 1
            stage.update(pid, focus=idx, flight=flight)
            speak(f"Here's {places[idx]['name']}.",
                  on_start=lambda: stage.highlight(pid, {"place": idx}, places[idx]["name"]))
            return f"Pointed at {places[idx]['name']}"
    except Exception as e:
        print(f"  point_out error: {e}")
        speak("I couldn't point that out.")
        return f"Point out failed: {e}"
    speak("I can't point at things in that panel.")
    return "Point out: unsupported panel"


# ── Files ─────────────────────────────────────────────────────────────────────
def _find_output_file(name):
    out = config.JARVIS_OUTPUT_DIR
    if not os.path.isdir(out):
        return None
    names = [n for n in os.listdir(out) if os.path.isfile(os.path.join(out, n))
             and media.guess_file_type(n) in ("code", "text")]
    if not names:
        return None
    if not name:     # most recent
        return os.path.join(out, max(names, key=lambda n: os.path.getmtime(os.path.join(out, n))))
    lower = {n.lower(): n for n in names}
    key = name.lower()
    hit = lower.get(key) or next((lower[n] for n in lower if key in n), None)
    if not hit:
        close = difflib.get_close_matches(key, list(lower), n=1, cutoff=0.4)
        hit = lower[close[0]] if close else None
    return os.path.join(out, hit) if hit else None


@action("show_file",
        schema='{"type":"show_file","name":"<file name in jarvis_output, or empty for the latest>"}',
        rules=['"show me the file X", "open X in the editor", "pull up the report" (files Jarvis wrote) → type "show_file" — ONLY for viewing. Any request to change, edit, rewrite, add to or fix a file → "edit_file" instead, with the file name in "name".'])
def _show_file(a, chain):
    path = _find_output_file(a.get("name", ""))
    if not path:
        speak("I couldn't find that file in my output folder.")
        return "Show file: not found"
    stage.open_file(path)
    return f"Showing {os.path.basename(path)}"


@action("edit_file",
        schema='{"type":"edit_file","panel":"<panel number/title, or empty>","name":"<file name in jarvis_output if it is not on the stage, or empty>","instruction":"<the change to make>"}',
        rules=['"change/edit/rewrite/fix [part] of the file/report/document", "add a section about X to the document" → type "edit_file" (it opens the file itself — never answer an edit request with show_file). The change appears as a diff to accept or reject.',
               '"accept the changes", "keep it" → type "file_changes" with decision "accept"; "reject the changes", "undo that" → decision "reject"'])
def _edit_file(a, chain):
    pid = stage.resolve(a.get("panel") or a.get("name") or "file")
    p = stage.get(pid) if pid else None
    if not p or p["kind"] not in ("file", "note"):
        # Only an actual file name may pick a file from jarvis_output — never a panel number
        path = _find_output_file(a.get("name", "")) if a.get("name") else None
        if not path:
            speak("Which file? Open it on the stage first.")
            return "Edit file: no file"
        pid = stage.open_file(path)
        p = stage.get(pid)
    is_note = p["kind"] == "note"
    current = p["data"].get("markdown" if is_note else "content", "")
    speak("Working on the changes.")
    try:
        resp = config.client.messages.create(
            model=config.MODEL, max_tokens=6000,
            system=(f"You edit the {'Markdown note' if is_note else 'file'} '{p['title']}'. Apply the user's "
                    "instruction and output ONLY the complete new content — no explanation, no code fences. "
                    "Keep everything the instruction doesn't ask to change exactly as it is."),
            messages=[{"role": "user", "content": f"Instruction: {a.get('instruction', '')}\n\n"
                                                  f"Current content:\n{current}"}])
        new = resp.content[0].text
        new = re.sub(r"^```[^\n]*\n|\n```\s*$", "", new.strip()) + "\n"
    except Exception as e:
        speak("Editing failed.")
        return f"Edit file failed: {e}"
    if is_note:      # notes aren't files: just update the card
        stage.update(pid, markdown=new)
        speak("Updated.")
        return f"Edited note {p['title']}"
    stage.propose_file(pid, new, a.get("instruction", ""))
    speak("Done. The changes are highlighted on the stage — accept or reject them.")
    return f"Proposed edit to {p['title']}"


@action("file_changes", schema='{"type":"file_changes","decision":"accept|reject","panel":"<or empty>"}')
def _file_changes(a, chain):
    pid = stage.resolve(a.get("panel") or "file")
    accept = a.get("decision", "accept") == "accept"
    if pid and stage.resolve_proposal(pid, accept):
        speak("Changes saved." if accept else "Changes discarded.")
        return "File changes " + ("accepted" if accept else "rejected")
    speak("There are no pending changes.")
    return "File changes: none pending"


# ── Images ────────────────────────────────────────────────────────────────────
def _image_files():
    roots = [config.JARVIS_OUTPUT_DIR, config.JARVIS_INPUT_DIR]
    found = []
    for root in roots:
        for dirpath, _, names in os.walk(root):
            found += [os.path.join(dirpath, n) for n in names
                      if os.path.splitext(n)[1].lower() in media.IMAGE_EXTS]
    return found


@action("show_image",
        schema='{"type":"show_image","name":"<image name or description, or empty for the latest>"}',
        rules=['"show me the screenshot/image/picture", "show the image I gave you" → type "show_image"'])
def _show_image(a, chain):
    images = _image_files()
    if not images:
        speak("I don't have any images yet.")
        return "Show image: none"
    name = (a.get("name") or "").lower()
    pick = None
    if name and name not in ("latest", "last", "recent"):
        by_name = {os.path.basename(p).lower(): p for p in images}
        hit = next((by_name[n] for n in by_name if name in n), None) or \
            next(iter(difflib.get_close_matches(name, list(by_name), n=1, cutoff=0.3)), None)
        pick = by_name.get(hit, hit) if hit else None
        if not pick and "screenshot" in name:
            shots = [p for p in images if "screenshot" in os.path.basename(p).lower()]
            pick = max(shots, key=os.path.getmtime) if shots else None
    pick = pick or max(images, key=os.path.getmtime)
    stage.add_image(pick)
    return f"Showing {os.path.basename(pick)}"


# ── Notes ─────────────────────────────────────────────────────────────────────
@action("show_note",
        schema='{"type":"show_note","title":"<short title>","prompt":"<what to put on the card>"}',
        rules=['"put X on the stage/screen", "show me a list/table/steps/comparison of X" (something to look at, not a saved file) → type "show_note"'])
def _show_note(a, chain):
    title = a.get("title") or "Note"
    try:
        resp = config.client.messages.create(
            model=config.MODEL, max_tokens=1500,
            system=("Write concise, well-structured Markdown for a panel on a display. Use headings, "
                    "bullet lists and tables where they help. No preamble, no closing remarks."),
            messages=[{"role": "user", "content": a.get("prompt", title)}])
        md = resp.content[0].text.strip()
    except Exception as e:
        speak("I couldn't put that together.")
        return f"Show note failed: {e}"
    stage.add("note", title, {"markdown": md})
    speak(f"{title} is on the stage.")
    return f"Note: {title}"


# ── Map ───────────────────────────────────────────────────────────────────────
_geo_cache: dict = {}
_geo_lock = threading.Lock()
_geo_last = [0.0]


def geocode(name):
    """Place name → {"name","lat","lon"} via OpenStreetMap Nominatim (max 1 request/s)."""
    key = name.strip().lower()
    if key in _geo_cache:
        return _geo_cache[key]
    with _geo_lock:
        wait = 1.05 - (time.time() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        _geo_last[0] = time.time()
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"q": name, "format": "json", "limit": 1},
                             headers={"User-Agent": "JARVIS-personal-assistant/1.0"}, timeout=8)
            hits = r.json()
        except Exception as e:
            print(f"  Geocoding failed: {e}")
            return None
    if not hits:
        return None
    place = {"name": name.strip(), "lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]),
             "detail": hits[0].get("display_name", "")}
    try:     # Nominatim gives [south, north, west, east]; the globe frames it as [w, s, e, n]
        s, n, w, e = (float(v) for v in hits[0]["boundingbox"])
        place["bbox"] = [w, s, e, n]
    except (KeyError, ValueError):
        pass
    _geo_cache[key] = place
    return place


@action("show_map",
        schema='{"type":"show_map","places":["<place name>"],"route":false,"title":"<short title or empty>"}',
        rules=['"show me X on the map", "where is X", "show me the globe", "map of X and Y" → type "show_map" with the place names (route true to connect them in order). Never use the user\'s own location.'])
def _show_map(a, chain):
    names = [n for n in a.get("places", []) if isinstance(n, str) and n.strip()][:8]
    places = [p for p in (geocode(n) for n in names) if p]
    if names and not places:
        speak("I couldn't find those places.")
        return "Show map: nothing found"
    stage.show_places(places, bool(a.get("route")), a.get("title") or (", ".join(p["name"] for p in places) or "Globe"))
    if places:
        speak(f"Here's {places[-1]['name']}." if len(places) == 1 else f"Showing {len(places)} places.")
    return f"Map: {[p['name'] for p in places]}"


# ── Layout by voice ───────────────────────────────────────────────────────────
@action("stage",
        schema='{"type":"stage","command":"show|hide|clear|restore|close|focus|arrange|resize|move|swap|clear_highlights","panel":"<panel number/kind/title, or empty>","other":"<second panel, for swap>","mode":"auto|columns|rows","size":"small|medium|large|huge|bigger|smaller","position":"left|right|top|bottom"}',
        rules=['"show/open the stage" → stage "show"; "back to the dashboard", "hide the stage" → "hide"',
               '"close the map", "close panel 2" → stage "close" with panel; "clear the stage", "reset the stage", "start fresh" → "clear"',
               '"undo that", "bring it back", "restore the stage", "reopen what I closed" (after closing panels or clearing) → stage "restore"',
               '"focus on X", "make X full screen" → stage "focus"; "show everything", "tile them", "side by side" → stage "arrange" (mode auto, or columns/rows)',
               '"make X bigger/smaller/huge" → stage "resize"; "move X to the left/right" → stage "move"; "swap 1 and 3" → stage "swap"',
               '"clear the highlights", "stop pointing" → stage "clear_highlights"'])
def _stage(a, chain):
    cmd, ref = a.get("command", ""), a.get("panel", "")
    if cmd == "show":
        ensure_visible()
        return "Stage shown"
    if cmd == "hide":
        stage.navigate("/")
        return "Stage hidden"
    if cmd == "clear":
        stage.clear()
        speak("Stage cleared. Say restore the stage if you want it back.")
        return "Stage cleared"
    if cmd == "restore":
        n = stage.restore_removed()
        speak(f"Brought back {n} panel{'s' if n != 1 else ''}." if n else "There's nothing to bring back.")
        return f"Stage restored {n}"
    if cmd == "clear_highlights":
        stage.clear_highlights()
        return "Highlights cleared"
    ok = {
        "close":   lambda: stage.close(ref),
        "focus":   lambda: stage.arrange("focus", ref),
        "arrange": lambda: stage.arrange(a.get("mode") or "auto"),
        "resize":  lambda: stage.resize(ref, a.get("size", "bigger")),
        "move":    lambda: stage.move(ref, a.get("position", "left")),
        "swap":    lambda: stage.swap(ref, a.get("other", "")),
    }.get(cmd, lambda: False)()
    if not ok:
        speak("I couldn't find that panel." if cmd in ("close", "focus", "resize", "move", "swap")
              else "I'm not sure how to arrange that.")
        return f"Stage {cmd}: failed"
    stage.navigate("/stage")
    return f"Stage {cmd}"


def ensure_visible():
    """Switch to the Stage (stage.navigate opens the desktop app if no dashboard is connected)."""
    stage.navigate("/stage")
