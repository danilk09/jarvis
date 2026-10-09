"""
Coding projects with Claude Code.

"Code me X" creates a folder in jarvis_coding/ and Claude Code builds it. Later
requests ("add dark mode", "fix the login bug") go straight to Claude Code in the
current project, continuing the same conversation, so it keeps its context.

Claude Code runs headless in the background (claude -p --output-format stream-json).
Its progress streams onto a "code" panel on the Stage, and Jarvis speaks Claude
Code's own summary when it finishes.

Permissions (config.CODE_PERMISSION_MODE, default acceptEdits): file edits — and
file operations inside the project — happen automatically; shell commands must
match config.CODE_ALLOWED_TOOLS. Anything else is refused and reported, and you can
approve it by voice ("allow it"), which resumes the session with that command allowed.

Preferences live in jarvis_coding/CLAUDE.md. Claude Code reads CLAUDE.md from the
project folder and every folder above it, so they apply to every project automatically.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import brain, config, stage, verbosity
from .registry import action
from .tts import speak

PREFS_FILE  = os.path.join(config.CODING_DIR, "CLAUDE.md")
_STATE_FILE = os.path.join(config.ROOT, "coding_state.json")
_EFFORTS    = ("low", "medium", "high", "xhigh", "max")
_MODELS     = ("opus", "sonnet", "fable", "haiku")
_MAX_EVENTS = 300

# Appended to Claude Code's system prompt for every Jarvis-driven run
_JARVIS_NOTE = (
    "You are being driven by voice through JARVIS, the user's voice assistant. The user is not "
    "watching a terminal and cannot answer questions mid-task, so make sensible decisions yourself "
    "and mention any important ones at the end. When you finish, end with a plain-language "
    "summary — no markdown, no file lists — of what you did and anything the user needs to do "
    "next. It will be read aloud. "
)

_PREFS_TEMPLATE = """# Coding preferences

Claude Code reads this file for every project in this folder (jarvis_coding/), so
everything here applies to all of them. Edit it freely, or tell Jarvis:
"remember I prefer ..." to add a line.

## Preferences
"""

brain.context_providers.append(lambda: context())

_lock  = threading.RLock()
_state = {"current": None, "projects": {}}   # projects: name -> {"path","session","pending","created","last"}
_job   = None                                 # the running Claude Code process and its bookkeeping


# ── State ─────────────────────────────────────────────────────────────────────
def init():
    """Load saved projects, make sure the preferences file exists, tidy interrupted panels."""
    global _state
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            _state = {"current": None, "projects": {}, **json.load(f)}
    except (OSError, ValueError):
        pass
    ensure_prefs()
    for p in stage.panels():                 # a run can't survive a restart
        if p["kind"] == "code" and p["data"].get("status") == "running":
            stage.update(p["id"], status="interrupted")


def _save():
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except OSError as e:
        print(f"  Could not save coding state: {e}")


def ensure_prefs():
    os.makedirs(config.CODING_DIR, exist_ok=True)
    if not os.path.exists(PREFS_FILE):
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            f.write(_PREFS_TEMPLATE)


def remember_pref(text):
    """Add a preference line to jarvis_coding/CLAUDE.md."""
    ensure_prefs()
    text = text.strip().rstrip(".")
    with open(PREFS_FILE, encoding="utf-8") as f:
        body = f.read()
    with open(PREFS_FILE, "a", encoding="utf-8") as f:
        f.write(("" if body.endswith("\n") else "\n") + f"- {text[0].upper() + text[1:]}\n")
    for p in stage.panels():                  # refresh it if it's open on the Stage
        if p["kind"] == "file" and stage.private(p["id"]).get("path") == PREFS_FILE:
            stage.open_file(PREFS_FILE)


def current():
    with _lock:
        name = _state["current"]
        return (name, _state["projects"][name]) if name in _state["projects"] else (None, None)


def context():
    """A line for Claude's system prompt so "add dark mode" goes to the right project."""
    name, proj = current()
    lines = []
    if name:
        lines.append(f'Current coding project: "{name}" ({proj["path"]})')
    if _job and _job["proc"].poll() is None:
        lines.append(f'Claude Code is working on "{_job["project"]}" right now: {_job["request"][:120]}')
    if proj and proj.get("pending"):
        lines.append("Waiting for permission to run: " + "; ".join(d["command"] for d in proj["pending"]))
    others = [n for n in _state["projects"] if n != name][:12]
    if others:
        lines.append("Other projects: " + ", ".join(others))
    return ("\n\nCODING:\n" + "\n".join(lines)) if lines else ""


# ── Projects ──────────────────────────────────────────────────────────────────
def _slug(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    stop = {"a", "an", "the", "me", "for", "that", "with", "and", "app", "make", "build", "code", "create"}
    picked = [w for w in words if w not in stop][:4] or ["project"]
    return "_".join(picked)


def _find_project(name):
    """Match a spoken project name against known projects and folders in jarvis_coding/."""
    if not name:
        return None
    key = _slug(name)
    with _lock:
        known = dict(_state["projects"])
    if os.path.isdir(config.CODING_DIR):
        for d in os.listdir(config.CODING_DIR):
            full = os.path.join(config.CODING_DIR, d)
            if os.path.isdir(full) and not d.startswith(".") and d not in known:
                known[d] = {"path": full, "session": None}
    if key in known:
        return key, known[key]
    for n in known:                           # partial match: "budget" → "budget_tracker"
        if key in n or n in key or all(w in n for w in key.split("_")):
            return n, known[n]
    import difflib
    close = difflib.get_close_matches(key, list(known), n=1, cutoff=0.5)
    return (close[0], known[close[0]]) if close else None


def _new_project(name):
    base = _slug(name)
    folder, n = base, 2
    while os.path.exists(os.path.join(config.CODING_DIR, folder)):
        folder, n = f"{base}_{n}", n + 1
    path = os.path.join(config.CODING_DIR, folder)
    os.makedirs(path)
    return folder, path


def _use(name, proj):
    with _lock:
        entry = _state["projects"].setdefault(name, {"path": proj["path"], "session": None,
                                                     "pending": [], "created": time.time()})
        entry["path"] = proj["path"]
        _state["current"] = name
        _save()
        return entry


# ── Running Claude Code ───────────────────────────────────────────────────────
def claude_exe():
    """The real claude.exe (npm installs a .cmd shim, which Python can't launch cleanly)."""
    if config.CLAUDE_EXE and os.path.isfile(config.CLAUDE_EXE):
        return config.CLAUDE_EXE
    found = shutil.which("claude")
    if not found:
        return None
    if found.lower().endswith((".cmd", ".bat")):
        try:
            with open(found, encoding="utf-8", errors="ignore") as f:
                m = re.search(r'"%dp0%\\([^"]+\.exe)"', f.read())
            if m:
                exe = os.path.join(os.path.dirname(found), m.group(1))
                if os.path.isfile(exe):
                    return exe
        except OSError:
            pass
    return found


def busy():
    return bool(_job and _job["proc"].poll() is None)


def _code_panel(name, path):
    """The project's Claude Code panel on the Stage (reused across runs)."""
    for p in stage.panels():
        if p["kind"] == "code" and p["data"].get("path") == path:
            return p["id"]
    return stage.add("code", f"Claude Code — {name}", {"project": name, "path": path, "status": "idle",
                                                        "events": [], "files": [], "runs": 0})


def start(request, project=None, new=False, effort="", model="", plan=False, extra_allowed=(), quiet=False):
    """Send a request to Claude Code in a project (in the background). Returns a status string."""
    global _job
    exe = claude_exe()
    if not exe:
        speak("Claude Code isn't installed. Install it with npm install -g @anthropic-ai/claude-code.")
        return "Claude Code not installed"
    if busy():
        speak(f"Claude Code is still working on {_job['project']}. Say stop coding to cancel it, "
              "or wait until it's done.")
        return "Busy"

    if new:
        name, path = _new_project(project or request)
        proj = _use(name, {"path": path})
    elif project:
        hit = _find_project(project)
        if not hit:
            speak(f"I couldn't find a project called {project}.")
            return "Project not found"
        name, proj = hit[0], _use(*hit)
    else:
        name, proj = current()
        if not name:
            speak("Which project? Tell me to start a new one, or say switch to and the project name.")
            return "No current project"

    effort = effort if effort in _EFFORTS else config.CODE_EFFORT
    model = model if (model in _MODELS or model.startswith("claude-")) else config.CODE_MODEL
    mode = "plan" if plan else config.CODE_PERMISSION_MODE
    allowed = list(config.CODE_ALLOWED_TOOLS) + list(extra_allowed)

    argv = [exe, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", mode, "--append-system-prompt",
            _JARVIS_NOTE + verbosity.rule(short="Keep that summary to one or two short sentences, about 30 words.")]
    if proj.get("session"):
        argv += ["--resume", proj["session"]]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    argv += ["--allowedTools", *allowed]
    prompt = request
    if new:
        prompt = (f"{request}\n\nThis is a new, empty project folder: build it from scratch here. "
                  "Follow the coding preferences in CLAUDE.md.")

    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}
    if not config.CODE_USE_API_KEY:
        # .env's key (loaded for Jarvis's own calls) would override the user's Claude login
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    try:
        proc = subprocess.Popen(argv, cwd=proj["path"], env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError as e:
        speak("I couldn't start Claude Code.")
        return f"Claude Code failed to start: {e}"

    pid = _code_panel(name, proj["path"])
    p = stage.get(pid)
    runs = p["data"].get("runs", 0) + 1
    divider = {"kind": "request", "text": request, "time": time.time(),
               "settings": {"mode": mode, "effort": effort or "default", "model": model or "default"}}
    events = (p["data"].get("events", []) + [divider])[-_MAX_EVENTS:]
    stage.update(pid, status="running", request=request, events=events, runs=runs, summary="",
                 denials=[], settings=divider["settings"], started=time.time())
    with _lock:
        proj["pending"] = []
        _save()
    _job = {"proc": proc, "project": name, "panel": pid, "request": request, "events": events,
            "files": list(p["data"].get("files", [])), "dirty": False, "plan": plan, "quiet": quiet,
            "commands": {}, "succeeded": set()}
    threading.Thread(target=_read_output, args=(_job,), daemon=True, name="claude-code").start()
    threading.Thread(target=_flush_loop, args=(_job,), daemon=True).start()
    print(f"  Claude Code started in {proj['path']} ({mode}, effort={effort or 'default'}, model={model or 'default'})")
    return f"Claude Code working on {name}"


def _short_path(path, root):
    try:
        rel = os.path.relpath(path, root)
        return path if rel.startswith("..") else rel
    except ValueError:
        return path


def _add_event(job, kind, text, **extra):
    job["events"].append({"kind": kind, "text": text[:600], "time": time.time(), **extra})
    del job["events"][:-_MAX_EVENTS]
    job["dirty"] = True


def _flush_loop(job):
    """Push progress to the Stage at most ~3 times a second (each update re-sends the state)."""
    while job["proc"].poll() is None or job["dirty"]:
        if job["dirty"]:
            job["dirty"] = False
            stage.update(job["panel"], events=list(job["events"]), files=list(job["files"]))
        time.sleep(0.35)


def _read_output(job):
    proc = job["proc"]
    root = (stage.get(job["panel"]) or {}).get("data", {}).get("path", "")
    result = None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            _add_event(job, "log", line)
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            with _lock:
                entry = _state["projects"].get(job["project"])
                if entry:
                    entry["session"] = ev.get("session_id") or entry.get("session")
                    _save()
        elif kind == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    _add_event(job, "text", block["text"].strip())
                elif block.get("type") == "tool_use":
                    _tool_event(job, block.get("name", ""), block.get("input") or {}, root)
                    if block.get("name") in ("PowerShell", "Bash"):
                        job["commands"][block.get("id")] = (block.get("input") or {}).get("command", "")
        elif kind == "user":
            for block in ev.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error"):
                    cmd = job["commands"].get(block.get("tool_use_id"))
                    if cmd:
                        job["succeeded"].add(cmd.strip())
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)[:300]
                    _add_event(job, "error", text)
        elif kind == "result":
            result = ev
    proc.wait()
    _finish(job, result)


def _tool_event(job, name, inp, root):
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        rel = _short_path(path, root)
        if rel and rel != path and rel not in job["files"]:      # only files inside the project
            job["files"].append(rel)
        _add_event(job, "edit", rel, path=path, op="created" if name == "Write" else "edited")
    elif name in ("PowerShell", "Bash"):
        _add_event(job, "command", inp.get("command", ""), note=inp.get("description", ""))
    elif name in ("Task", "Agent"):
        _add_event(job, "agent", inp.get("description") or inp.get("prompt", "")[:120],
                   agent=inp.get("subagent_type", ""))
    elif name in ("Read", "Glob", "Grep", "LS"):
        target = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
        _add_event(job, "read", f"{name} {_short_path(target, root)}")
    elif name == "TodoWrite":
        todos = inp.get("todos") or []
        _add_event(job, "todo", "Plan: " + " · ".join(t.get("content", "") for t in todos[:8]))
    elif name in ("WebFetch", "WebSearch"):
        _add_event(job, "read", f"{name} {inp.get('url') or inp.get('query', '')}")
    else:
        _add_event(job, "tool", name)


def _speakable(text, limit=450):
    """Claude Code's closing summary, trimmed to something comfortable to hear."""
    text = re.sub(r"[`*_#>]", "", text or "").strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    text = paragraphs[-1] if paragraphs else text          # the summary comes last
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[:cut.rfind(". ") + 1] or cut


def _finish(job, result):
    global _job
    stopped = job.get("stopped")
    status = "stopped" if stopped else "error" if (not result or result.get("is_error")) else \
             ("planned" if job["plan"] else "done")
    summary = (result or {}).get("result", "") or ("Stopped." if stopped else "Claude Code stopped unexpectedly.")
    denials = [{"tool": d.get("tool_name", ""), "command": (d.get("tool_input") or {}).get("command", "")
                or json.dumps(d.get("tool_input") or {})[:200]}
               for d in (result or {}).get("permission_denials", [])]
    # Claude Code's PowerShell permission check can time out and refuse a command that then
    # succeeds on retry; only report what really didn't run
    denials = [d for d in denials if d["command"].strip() not in job["succeeded"]]
    job["dirty"] = False
    stage.update(job["panel"], status=status, summary=summary, denials=denials,
                 events=list(job["events"]), files=list(job["files"]),
                 # on a Claude subscription the reported cost is only notional, so don't show it
                 cost=(result or {}).get("total_cost_usd") if config.CODE_USE_API_KEY else None,
                 duration=(result or {}).get("duration_ms"))
    with _lock:
        entry = _state["projects"].get(job["project"])
        if entry:
            entry["pending"] = denials
            entry["last"] = time.time()
            if result and result.get("session_id"):
                entry["session"] = result["session_id"]
            _save()
        if _job is job:
            _job = None

    # Files Jarvis has open in the editor may have just changed on disk
    path = (stage.get(job["panel"]) or {}).get("data", {}).get("path", "")
    for p in stage.panels():
        fp = stage.private(p["id"]).get("path", "")
        try:
            inside = bool(fp and path) and os.path.commonpath([fp, path]) == path
        except ValueError:      # different drives
            inside = False
        if p["kind"] == "file" and inside and os.path.isfile(fp):
            stage.open_file(fp)

    brain.remember(f"[Asked Claude Code in project {job['project']}] {job['request']}",
                   f"[Claude Code result] {summary[:1500]}")
    if stopped or job["quiet"]:
        return
    if status == "error":
        speak("Claude Code ran into a problem. The details are on the stage.")
    elif status == "planned":
        speak("The plan is ready on the stage. Say go ahead to build it, or tell me what to change.")
    else:
        speak(_speakable(summary))
    if denials:
        first = _speakable_command(denials[0]["command"])
        more = f" and {len(denials) - 1} other command{'s' if len(denials) > 2 else ''}" if len(denials) > 1 else ""
        speak(f"It also wanted to run {first}{more}, which needs your permission. "
              "Say allow it if that's okay — the commands are on the stage.")


def use_path(path):
    """Make an existing folder (anywhere) the current project."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return None
    name = os.path.basename(path.rstrip("\\/")) or "project"
    _use(name, {"path": path})
    return name


def _speakable_command(cmd):
    """A refused command, short enough to say: the last part, paths cut down to file names."""
    part = re.split(r"\s*(?:;|&&)\s*", cmd.strip())[-1]
    part = re.sub(r'"?(?:[A-Za-z]:)?[\\/][^"\s]*[\\/]([^"\\/\s]+)"?', r"\1", part)
    return part[:80]


def stop():
    if not busy():
        return False
    _job["stopped"] = True
    subprocess.run(["taskkill", "/PID", str(_job["proc"].pid), "/T", "/F"], capture_output=True)
    return True


def approve():
    """Re-run with the commands Claude Code was refused, now allowed."""
    name, proj = current()
    if not proj or not proj.get("pending"):
        speak("Nothing is waiting for permission.")
        return "Nothing pending"
    pending = proj["pending"]
    # Claude Code checks each part of a combined command ("cd x; python y") separately,
    # so allow every part — by its program and first argument — not the whole string
    allowed = []
    for d in pending:
        tool = d["tool"] or "PowerShell"
        for part in re.split(r"\s*(?:;|&&|\|\||\|)\s*", d["command"]):
            words = part.strip().split()
            if words:
                prefix = " ".join(words[:2]) if len(words) > 1 and not words[1].startswith(("-", '"', "'")) else words[0]
                allowed.append(f"{tool}({prefix}:*)")
    cmds = "\n".join(f"- {d['command']}" for d in pending)
    speak("Okay, letting it run those.")
    return start(f"The user approved these commands that were refused before:\n{cmds}\n"
                 "Run them now and finish the task.", extra_allowed=allowed)


def open_vscode():
    name, proj = current()
    if not proj:
        return False
    subprocess.Popen(["code", proj["path"]], shell=True)
    return True


def open_terminal():
    """Take over in an interactive Claude Code window, continuing the same conversation."""
    name, proj = current()
    exe = claude_exe()
    if not proj or not exe:
        return False
    resume = f" --resume {proj['session']}" if proj.get("session") else ""
    q = proj["path"].replace("'", "''")
    subprocess.Popen(["powershell", "-NoExit", "-Command",
                      f"Set-Location -LiteralPath '{q}'; & '{exe}'{resume}"],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True


# ── Voice ─────────────────────────────────────────────────────────────────────
@action("code",
        schema='{"type":"code","command":"new|run|switch|list|status|stop|approve|remember|show_prefs|open_vscode|open_terminal","request":"<the full coding request, in the user\'s words>","project":"<project name, or empty for the current one>","effort":"<low|medium|high|xhigh|max, or empty>","model":"<opus|sonnet|fable|haiku, or empty>","plan_only":false,"preference":"<for remember>"}',
        rules=['"code/build/make/write me a [app, script, game, project]", "start a new project for X" → code command "new"; "request" = the full description; "project" = a short name for it',
               'ANY change to the current coding project ("add dark mode", "fix the login bug", "write tests", "refactor X", "why does it crash") → code command "run" — it goes straight to Claude Code. Keep the user\'s wording in "request", including things like "use subagents" or "in parallel". This is for project code; files Jarvis wrote to the Stage use edit_file.',
               'Thinking depth: "think hard/harder" → effort "high"; "think really hard", "ultrathink", "max effort", "as thorough as possible" → "max"; "quick", "low effort" → "low". "use Opus/Sonnet/Haiku" → model. Leave both empty otherwise.',
               '"plan it first", "just plan", "don\'t change anything yet" → plan_only true. After a plan, "go ahead", "build it", "do it" → code command "run" with request "Implement the plan you proposed."',
               '"switch to / work on [project]" → code "switch" with project; "what projects do I have" → "list"; "how\'s the coding going", "what is Claude Code doing" → "status"; "stop coding", "cancel the coding" → "stop"',
               '"allow it", "yes run it" (after Jarvis asked permission for a command) → code "approve"',
               '"remember I prefer X for coding", "always use X in my projects", "from now on use X" → code "remember" with preference = the preference as a short sentence; "show my coding preferences" → "show_prefs"',
               '"open the project in VS Code" → code "open_vscode"; "open a Claude Code terminal", "let me take over" → "open_terminal"'])
def _code(a, chain):
    cmd = a.get("command") or "run"
    request = (a.get("request") or "").strip()
    context = chain.get("file_context") or chain.get("image_context") or ""
    if context and request:
        request += f"\n\nContext Jarvis gathered for this request:\n{context[:6000]}"

    if cmd in ("new", "run"):
        if not request:
            speak("What should it build?")
            return "Code: no request"
        if cmd == "new":
            speak("Starting a new project. Claude Code is on it.")
        else:
            speak("Handing that to Claude Code.")
        return start(request, project=a.get("project") or None, new=(cmd == "new"),
                     effort=(a.get("effort") or "").lower(), model=(a.get("model") or "").lower(),
                     plan=bool(a.get("plan_only")))
    if cmd == "switch":
        hit = _find_project(a.get("project", ""))
        if not hit:
            speak(f"I couldn't find a project called {a.get('project', '')}.")
            return "Code: project not found"
        _use(*hit)
        speak(f"Now working on {hit[0].replace('_', ' ')}.")
        return f"Current project: {hit[0]}"
    if cmd == "list":
        names = list(_state["projects"]) or [d for d in os.listdir(config.CODING_DIR)
                                             if os.path.isdir(os.path.join(config.CODING_DIR, d))]
        speak("Your projects are " + ", ".join(n.replace("_", " ") for n in names[:10]) + "." if names
              else "You don't have any coding projects yet.")
        return "Code: listed"
    if cmd == "status":
        name, proj = current()
        if busy():
            last = next((e["text"] for e in reversed(_job["events"]) if e["kind"] in ("edit", "command", "text")), "")
            speak(f"Claude Code is working on {_job['project'].replace('_', ' ')}. Latest: {last[:160]}")
        elif name:
            speak(f"Claude Code is idle. The current project is {name.replace('_', ' ')}.")
        else:
            speak("There's no coding project yet.")
        return "Code: status"
    if cmd == "stop":
        speak("Stopping Claude Code." if stop() else "Claude Code isn't running.")
        return "Code: stop"
    if cmd == "approve":
        return approve()
    if cmd == "remember":
        pref = (a.get("preference") or request).strip()
        if not pref:
            speak("What should I remember?")
            return "Code: no preference"
        remember_pref(pref)
        speak("Got it. I'll pass that on to Claude Code for every project.")
        return f"Preference saved: {pref}"
    if cmd == "show_prefs":
        ensure_prefs()
        stage.open_file(PREFS_FILE)
        return "Showing coding preferences"
    if cmd == "open_vscode":
        speak("Opening it in VS Code." if open_vscode() else "There's no current project.")
        return "Code: VS Code"
    if cmd == "open_terminal":
        speak("Opening Claude Code in a terminal." if open_terminal() else "There's no current project.")
        return "Code: terminal"
    speak("I'm not sure what to do with that project.")
    return f"Code: unknown command {cmd}"
