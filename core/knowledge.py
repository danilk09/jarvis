"""
Exact answers and video transcripts, fetched directly (no MCP server, no tool loop).

  - wolfram  Wolfram|Alpha's Spoken Results API returns one ready-to-say sentence, so most
             answers cost no Claude tokens at all; when it has no short answer, the LLM API's
             text result is boiled down by one Haiku call.
  - youtube  the video's captions through yt-dlp (already used for music), summarized or
             questioned by one Haiku call. The full transcript is saved to
             jarvis_output/transcripts/ so a follow-up ("make notes of it in a Google Doc")
             can hand it on.
"""

import os
import re

import requests

from . import brain, config, stage, verbosity
from .registry import action
from .tts import speak

_TRANSCRIPT_DIR = os.path.join(config.JARVIS_OUTPUT_DIR, "transcripts")
_MAX_TRANSCRIPT = 150_000        # characters sent to Haiku (~2.5 hours of speech)


# ── Wolfram|Alpha ─────────────────────────────────────────────────────────────
def wolfram(query):
    """(spoken answer, full text or "")."""
    units = "imperial" if config.UNITS == "imperial" else "metric"
    r = requests.get("https://api.wolframalpha.com/v1/spoken", timeout=10,
                     params={"appid": config.WOLFRAM_APP_ID, "i": query, "units": units})
    if r.status_code == 200 and r.text.strip() and "did not understand" not in r.text:
        return r.text.strip(), ""
    r = requests.get("https://www.wolframalpha.com/api/v1/llm-api", timeout=15,
                     params={"appid": config.WOLFRAM_APP_ID, "input": query, "maxchars": 3000, "units": units})
    if r.status_code != 200:
        return "", ""
    full = r.text.strip()
    resp = config.client.messages.create(
        model=config.MODEL, **config.NO_THINK, max_tokens=verbosity.tokens(150, 400),
        system="Answer the user's question out loud from this Wolfram|Alpha result only. Plain spoken "
               "sentences, numbers as words would be read, no markdown. " + verbosity.rule(),
        messages=[{"role": "user", "content": f"Question: {query}\n\nWolfram|Alpha result:\n{full}"}])
    brain.log_usage("wolfram", resp.usage)
    return config.reply_text(resp).strip(), full


@action("wolfram",
        schema='{"type":"wolfram","query":"<the question, in plain English>"}',
        rules=['Exact computation or reference facts where precision matters → type "wolfram": math beyond mental arithmetic, solving equations, unit conversions, "how many days until X", physics/chemistry values, nutrition facts, astronomy, population/distance/elevation statistics. Opinions, explanations and general knowledge stay "chat".'])
def _wolfram(a, chain):
    query = (a.get("query") or "").strip()
    if not config.WOLFRAM_APP_ID:
        speak("Wolfram Alpha isn't set up. Add WOLFRAM_APP_ID to your .env file.")
        return "Wolfram: no AppID"
    try:
        answer, full = wolfram(query)
    except Exception as e:
        print(f"  [wolfram] failed: {e}")
        answer, full = "", ""
    if not answer:
        speak("Wolfram Alpha didn't have an answer for that.")
        return f"Wolfram: no answer for {query}"
    if full and len(full) > 300:
        stage.add("note", f"Wolfram|Alpha — {query[:50]}", {"markdown": f"```\n{full}\n```"})
    speak(answer)
    brain.remember(f"[Wolfram|Alpha] {query}", answer)
    return f"Wolfram: {answer[:80]}"


# ── YouTube transcripts ───────────────────────────────────────────────────────
def _captions(info):
    """Text of the best English caption track (manual first, then automatic)."""
    for tracks in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        lang = next((l for l in ("en", "en-US", "en-GB", "en-orig") if l in tracks), None) \
            or next((l for l in tracks if l.startswith("en")), None)
        if not lang:
            continue
        fmt = next((f for f in tracks[lang] if f.get("ext") == "json3"), None)
        if not fmt:
            continue
        data = requests.get(fmt["url"], timeout=20).json()
        words = []
        for ev in data.get("events", []):
            text = "".join(s.get("utf8", "") for s in ev.get("segs", []) or [])
            if text.strip():
                words.append(text.replace("\n", " ").strip())
        text = re.sub(r"\s+", " ", " ".join(words)).strip()
        if text:
            return text
    return ""


def _stage_video():
    """A YouTube page open on the Stage ("summarize this video")."""
    for p in reversed(stage.panels()):
        url = p["data"].get("url", "") if p["kind"] == "page" else ""
        if re.search(r"(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)", url):
            return url
    return ""


def transcript(url="", query=""):
    """(title, url, transcript, saved path). url empty → first search result for query."""
    from yt_dlp import YoutubeDL
    from .music import _JS_RUNTIMES
    target = url or f"ytsearch1:{query}"
    with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                    "js_runtimes": _JS_RUNTIMES}) as ydl:
        info = ydl.extract_info(target, download=False)
    if info.get("entries"):
        info = info["entries"][0]
    text = _captions(info)
    title = info.get("title", "video")
    page = info.get("webpage_url") or url
    path = ""
    if text:
        os.makedirs(_TRANSCRIPT_DIR, exist_ok=True)
        path = os.path.join(_TRANSCRIPT_DIR, re.sub(r'[\\/:*?"<>|]+', "", title)[:80].strip() + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{title}\n{page}\n\n{text}\n")
    return title, page, text, path


@action("youtube",
        schema='{"type":"youtube","url":"<video URL, or empty>","query":"<video to search for if no URL, or empty for the one on the Stage>","prompt":"<what the user wants: summary, key points, an answer about it>"}',
        rules=['"summarize this video", "what does this YouTube video say about X", "key points of [video]", "TL;DW" → type "youtube" (url if given; query to find it by title/channel; both empty = the video open on the Stage). Can be chained with generate_file or delegate (e.g. notes in a Google Doc).'])
def _youtube(a, chain):
    url = (a.get("url") or "").strip() or ("" if a.get("query") else _stage_video())
    query = (a.get("query") or "").strip()
    if not url and not query:
        speak("Which video?")
        return "YouTube: no video"
    speak("Reading the video's transcript.")
    try:
        title, page, text, path = transcript(url, query)
    except Exception as e:
        print(f"  [youtube] failed: {e}")
        speak("I couldn't get that video.")
        return f"YouTube failed: {e}"
    if not text:
        speak(f"{title} doesn't have English captions, so I can't read it.")
        return f"YouTube: no captions for {title}"
    want = (a.get("prompt") or "summarize it").strip()
    resp = config.client.messages.create(
        model=config.MODEL, **config.NO_THINK, max_tokens=verbosity.tokens(900, 1600),
        system=("You are JARVIS. From a YouTube transcript, do what the user asked. Return two parts separated "
                "by a line '---': first what you say out loud (plain spoken sentences, no markdown; "
                + verbosity.rule() + "), then a Markdown version for the screen: key points as bullets, "
                "each with its approximate place in the video if clear."),
        messages=[{"role": "user", "content": f"Request: {want}\nVideo: {title}\n\nTranscript:\n{text[:_MAX_TRANSCRIPT]}"}])
    brain.log_usage("youtube", resp.usage)
    out = config.reply_text(resp)
    spoken, _, md = out.partition("\n---")
    stage.add("note", title[:60], {"markdown": f"[{title}]({page})\n\n{md.strip() or spoken.strip()}"})
    chain["file_context"] = f"YouTube video: {title} ({page})\nFull transcript saved at: {path}\n\n{md.strip()[:3000]}"
    speak(spoken.strip())
    brain.remember(f"[YouTube] {title}: {want}", spoken.strip())
    return f"YouTube: {title}"
