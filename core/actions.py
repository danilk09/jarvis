"""
Built-in actions. Each @action registers a handler plus the schema/rule lines
that go into Claude's system prompt (see registry.py).
"""

import os
import subprocess
import threading
import time
import webbrowser
from urllib.parse import quote_plus

from . import agenda, agent, audio, brain, config, files, media, music, stage, state
from . import briefing, coding, memory, notify, places, stage_actions  # noqa: F401  (register their actions)
from .generate import generate_file_content, summarize_for_speech
from .registry import ACTIONS, action
from .tts import speak
from .web import brave_search, synthesize_search_answer


# ── Opening things ────────────────────────────────────────────────────────────
@action("workspace",
        schema='{"type":"workspace","target":"<n>"}',
        rules=['"open [workspace]" → type "workspace". Fuzzy match (Example: "311","three eleven","3-11" all match workspace "311")'])
def _workspace(a, chain):
    ok, msg = files.open_workspace(a.get("target", ""), state.workspaces)
    if not ok:
        speak(msg)
    return msg


@action("url", schema='{"type":"url","target":"<full url>"}')
def _url(a, chain):
    webbrowser.open(a.get("target", ""))
    return f"Opened {a.get('target', '')}"


@action("app",
        schema='{"type":"app","target":"<app name>"}',
        rules=['"open [app]" → type "app"'])
def _app(a, chain):
    target = a.get("target", "")
    if files.open_app(target):
        return f"Launched {target}"
    speak(f"I couldn't find an app called {target}.")
    return f"App not found: {target}"


@action("search", schema='{"type":"search","engine":"google|youtube|github","query":"<q>"}')
def _search(a, chain):
    engine = a.get("engine", "google")
    q = quote_plus(a.get("query", ""))
    urls = {
        "google":  f"https://google.com/search?q={q}",
        "youtube": f"https://youtube.com/results?search_query={q}",
        "github":  f"https://github.com/search?q={q}",
    }
    webbrowser.open(urls.get(engine, urls["google"]))
    return f"Searched {engine} for '{a.get('query', '')}'"


@action("vscode", schema='{"type":"vscode","target":"<path>"}')
def _vscode(a, chain):
    target = a.get("target", "")
    subprocess.Popen(["code", target] if target else ["code"], shell=True)
    return f"Opened VSCode{' at ' + target if target else ''}"


def _open_file_matches(matches, keyword):
    """Open a single match directly, or show a numbered list and ask which to open."""
    if not matches:
        speak(f"I couldn't find any files matching {keyword}.")
        return f"No files found matching '{keyword}'"
    if len(matches) == 1:
        os.startfile(matches[0])
        return f"Opened {matches[0]}"
    os.makedirs(config.JARVIS_OUTPUT_DIR, exist_ok=True)
    results_path = os.path.join(config.JARVIS_OUTPUT_DIR, "jarvis_matches.txt")
    with open(results_path, "w", encoding="utf-8") as f:
        for i, p in enumerate(matches, 1):
            f.write(f"{i}. {p}\n")
    os.startfile(results_path)
    speak(f"Found {len(matches)} files. List is open. Say the numbers you want opened.")
    nums = audio.listen_for_selection()
    if not nums:
        return "Selection cancelled."
    opened = [n for n in nums if 1 <= n <= len(matches)]
    for n in opened:
        os.startfile(matches[n - 1])
        time.sleep(0.3)
    return f"Opened {len(opened)} file(s): {', '.join(os.path.basename(matches[n - 1]) for n in opened)}"


@action("find_files",
        schema='{"type":"find_files","keyword":"<word>","extension":"<or empty>"}',
        rules=['"find files" or "open file" → type "find_files" with keyword'])
def _find_files(a, chain):
    keyword = a.get("keyword", a.get("target", ""))
    if os.path.exists(keyword):
        os.startfile(keyword)
        return f"Opened {keyword}"
    matches = files.fuzzy_match_files(keyword)
    ext = a.get("extension", "").lower().strip()
    if ext:
        ext = ext if ext.startswith(".") else "." + ext
        matches = [p for p in matches if p.lower().endswith(ext)]
    return _open_file_matches(matches, keyword)


ACTIONS["file"] = _find_files   # older alias the model sometimes uses


@action("bookmark",
        schema='{"type":"bookmark","target":"<keyword>"}',
        rules=['"open [bookmark keyword]" → type "bookmark"'])
def _bookmark(a, chain):
    target = a.get("target", "")
    match = files.find_bookmark(target)
    if match:
        webbrowser.open(match["url"])
        return f"Opened bookmark: {match['name']}"
    speak(f"No bookmark found matching {target}.")
    return f"No bookmark found matching '{target}'"


# ── Seeing and reading ────────────────────────────────────────────────────────
@action("screenshot",
        schema='{"type":"screenshot","prompt":"<what to analyze or do with the screenshot>"}',
        rules=['"screenshot","take a screenshot","capture screen","look at my screen","what\'s on my screen","look at this" → type "screenshot"; put intent in "prompt". Screenshots can be chained with generate_file or code to use the image as context.'])
def _screenshot(a, chain):
    speak("Taking a screenshot.")
    result = media.take_screenshot(a.get("prompt", ""))
    chain["image_context"] = result
    if media.last_screenshot:
        stage.add_image(media.last_screenshot, caption=result, title="Screenshot")
    print(f"\n  ── Screenshot Analysis ──────────────\n  {result}\n  ────────────────────────────────────\n")
    speak(result)
    return result


@action("input_folder",
        schema='{"type":"input_folder","prompt":"<what to do with the files in the input folder>"}',
        rules=['"process input folder","check input folder","analyze input","look at input files","enhance image","what\'s in the input folder","summarize input" → type "input_folder"; put intent in "prompt". Can be chained with generate_file or code to use folder contents as context. Only for reading files dropped into jarvis_input — renaming/moving/deleting files anywhere is "agent".'])
def _input_folder(a, chain):
    if not media.wants_enhancement(a.get("prompt", "")):   # upscaling announces itself
        speak("Processing input folder.")
    output = media.process_input_folder(a.get("prompt", ""))
    chain["file_context"] = output["context"]
    print(f"\n  ── Input Folder ─────────────────────\n  {output['speech']}\n  ────────────────────────────────────\n")
    speak(output["speech"])
    return "Input folder processed."


@action("web_search",
        schema='{"type":"web_search","query":"<search query>"}',
        rules=["Anything needing current/real-time info WITHOUT file generation: news, weather, sports scores, prices, recent events → type \"web_search\""])
def _web_search(a, chain):
    if not config.BRAVE_API_KEY:
        msg = "Web search isn't set up. Add BRAVE_API_KEY to your .env file."
        speak(msg)
        return msg
    query = a.get("query", a.get("target", ""))
    speak("Let me look that up.")
    answer = synthesize_search_answer(query, brave_search(query))
    print(f"\n  ── Web Search: {query} ──────────\n  {answer}\n  ────────────────────────────────────\n")
    speak(answer)
    brain.remember(f"[Web search] {query}", answer)
    return f"Web search: {query}"


# ── Making things ─────────────────────────────────────────────────────────────
def _chain_context(chain):
    return chain.get("file_context", "") or chain.get("image_context", "")


@action("generate_file",
        schema='{"type":"generate_file","filename":"<name.ext>","prompt":"<full description of what to write in the file>","search_query":"<targeted web search query, or empty string if no current data needed>"}',
        rules=['"write a [file]", "create a [file]", "generate [file]", "make a [file]" → type "generate_file"; filename must include an extension (.py, .txt, .md, .html, etc.); put the full description of what to write in "prompt"; if the file content requires current/real-time data (e.g. today\'s news, current prices, recent stats, live standings), set "search_query" to a targeted search query — otherwise leave it as an empty string ""'])
def _generate_file(a, chain):
    # The filename comes from the model — never let it point outside jarvis_output/
    filename     = os.path.basename(a.get("filename", "")) or "jarvis_output.txt"
    search_query = a.get("search_query", "")
    speak(f"Generating {filename}.")
    context = _chain_context(chain)
    if search_query and config.BRAVE_API_KEY:
        speak("Looking up current data first.")
        results = brave_search(search_query, count=6)
        if results:
            web = "\n".join(f"{r['title']}: {r['description']} ({r['url']})" for r in results)
            context = web + ("\n\n" + context if context else "")
    elif search_query:
        speak("Note: no Brave API key, so current data unavailable.")
    content = generate_file_content(a.get("prompt", ""), filename, context)
    os.makedirs(config.JARVIS_OUTPUT_DIR, exist_ok=True)
    path = os.path.join(config.JARVIS_OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    state.push_file(filename, content)
    stage.open_file(path)
    speak(summarize_for_speech(content, filename))
    return f"Generated {filename}"


# ── Music ─────────────────────────────────────────────────────────────────────
@action("music",
        schema=['{"type":"music","query":"<song, artist, or genre>","command":"play"}',
                '{"type":"music","command":"stop|pause|resume|skip|replay"}',
                '{"type":"music","command":"prev","n":<1-5>}'],
        rules=['"play [song/artist/genre]", "play some music", "play something" → type "music", command "play", query = what to play',
               '"stop music", "turn off music" → type "music", command "stop"',
               '"pause music", "pause" → type "music", command "pause"',
               '"resume music", "unpause music", "resume" → type "music", command "resume"',
               '"skip", "next song", "skip this song" → type "music", command "skip"',
               '"previous song", "go back", "last song", "play the previous song" → type "music", command "prev", n=1',
               '"play the previous [N]th song", "play the [N]th previous song", e.g. "play the previous 3rd song" → command "prev", n=N (1–5)',
               '"replay", "replay this", "play it again", "restart the song" → type "music", command "replay"'])
def _music(a, chain):
    command = a.get("command", "play")
    if command == "play":
        q = a.get("query", "")
        if not q:
            speak("What would you like to play?")
            return "Music: no query"
        speak(f"Playing {q}.")
        threading.Thread(target=music.play, args=(q,), daemon=True).start()
        return f"Music: playing {q}"
    if command == "stop":
        music.stop()
        speak("Stopping music.")
        return "Music: stopped"
    if command == "pause":
        if music.is_playing():
            music.pause()
            speak("Pausing music.")
        return "Music: paused"
    if command == "resume":
        if music.is_playing() or music.is_paused():
            music.resume()
            speak("Resuming music.")
        return "Music: resumed"
    if command == "skip":
        if music.is_playing() or music.is_paused():
            music.skip()
            speak("Skipping to next track.")
        else:
            speak("No music is playing.")
        return "Music: skipped"
    if command == "prev":
        n = int(a.get("n", 1))
        threading.Thread(target=music.prev, args=(n,), daemon=True).start()
        return f"Music: previous {n}"
    if command == "replay":
        if music.has_track():
            speak("Replaying current song.")
            threading.Thread(target=music.replay, daemon=True).start()
        else:
            speak("No song is currently playing.")
        return "Music: replay"
    return "Music: unknown command"


# ── Utilities ─────────────────────────────────────────────────────────────────
def _fmt_duration(seconds):
    parts = []
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {unit}{'s' if n != 1 else ''}")
    return " ".join(parts) or "0 seconds"


@action("timer",
        schema='{"type":"timer","seconds":<number>,"label":"<what it is for, or empty>"}',
        rules=['"set a timer for N minutes", "remind me in N minutes to X" → type "timer", seconds = total seconds, label = reminder text'])
def _timer(a, chain):
    seconds = max(1, int(float(a.get("seconds", 60))))
    agenda.add_timer(seconds, a.get("label", ""))   # on the agenda, so it survives a restart
    speak(f"Timer set for {_fmt_duration(seconds)}.")
    return f"Timer: {seconds}s {a.get('label', '')}".strip()


@action("agent",
        schema='{"type":"agent","task":"<complete description of what to do>"}',
        rules=['Anything to DO on the computer that no other action covers — change a setting, run a command, move/rename/organise files, check system info → type "agent" with the full task in "task". (Coding projects use type "code".) Never reply that you cannot do something on the PC; use "agent" instead. Prefer a specific action when one fits, and never use "agent" for questions you can answer in chat.'])
def _agent(a, chain):
    speak("On it.")
    try:
        result = agent.run_agent(a.get("task", ""), _chain_context(chain))
    except Exception as e:
        result = f"Something went wrong: {e}"
    speak(result)
    brain.remember(f"[Task] {a.get('task', '')}", result)
    return f"Agent: {result}"


@action("none")
def _none(a, chain):
    return "No action"


# ── Dispatch ──────────────────────────────────────────────────────────────────
def handle_response(response):
    """Run a parsed response: {"mode": "action", "actions": [...]}, {"mode": "chat", "reply": ...} or {"mode": "none"}."""
    # The model sometimes returns a bare action object without the wrapper
    if response.get("mode") in ACTIONS and response.get("mode") != "none":
        response = {"mode": "action", "actions": [response]}
    elif "type" in response and "mode" not in response:
        response = {"mode": "action", "actions": [response]}

    mode = response.get("mode", "none")
    if mode == "action":
        actions = [a for a in response.get("actions", []) if isinstance(a, dict)]
        if not actions:
            speak("I'm not sure what to do with that.")
            return
        chain = {}
        for a in actions:
            kind = a.get("type") or a.get("mode", "")
            handler = ACTIONS.get(kind)
            if handler is None:
                print(f"  Unknown action: {kind}")
                continue
            print(f"  Done: {handler(a, chain)}")
    elif mode == "chat":
        reply = response.get("reply", "I'm not sure how to answer that.")
        print(f"\n  ── JARVIS ──────────────────────────\n  {reply}\n  ────────────────────────────────────\n")
        speak(reply)
    elif mode == "none":
        print("  No action taken.")
    else:
        print(f"  Unknown mode: {mode}")
