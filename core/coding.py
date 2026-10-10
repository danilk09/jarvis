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
_jobs: dict = {}                              # project name -> running Claude Code process + bookkeeping


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
    for job in running():
        lines.append(f'Claude Code is working on "{job["project"]}" right now: {job["request"][:120]}')
    with _lock:
        waiting = {n: p["pending"] for n, p in _state["projects"].items() if p.get("pending")}
    for n, pending in waiting.items():
        lines.append(f'Waiting for permission in "{n}": ' + "; ".join(d["command"][:80] for d in pending))
    others = [n for n in _state["projects"] if n != name][:12]
    if others:
        lines.append("Other projects: " + ", ".join(others))
    repos = repo_names()
    if repos:
        lines.append("Repos (folders in " + ", ".join(config.REPO_DIRS) + "): " + ", ".join(repos[:60]))
    return ("\n\nCODING:\n" + "\n".join(lines)) if lines else ""


# ── Projects ──────────────────────────────────────────────────────────────────
def _slug(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    stop = {"a", "an", "the", "me", "for", "that", "with", "and", "app", "make", "build", "code", "create"}
    picked = [w for w in words if w not in stop][:4] or ["project"]
    return "_".join(picked)


def _flat(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _known():
    """Every project Jarvis knows plus every folder in jarvis_coding/ and REPO_DIRS: {name: entry}."""
    with _lock:
        known = {n: dict(p) for n, p in _state["projects"].items()}
    paths = {os.path.normcase(p["path"]) for p in known.values()}
    for root in [config.CODING_DIR, *config.REPO_DIRS]:
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            full = os.path.join(root, d)
            if os.path.isdir(full) and not d.startswith(".") and d not in known \
                    and os.path.normcase(full) not in paths:
                known[d] = {"path": full, "session": None}
    return known


def repo_names():
    return sorted({d for root in config.REPO_DIRS if os.path.isdir(root) for d in os.listdir(root)
                   if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")}, key=str.lower)


def _find_project(name):
    """Match a spoken name ("murphys next js") against projects and repo folders: exact
    ignoring spaces/dashes/case, then folders containing every word (the shortest wins:
    "murphys" → murphys, not murphys-react), then the closest spelling."""
    key = _flat(name)
    if not key:
        return None
    known = _known()
    flat = {n: _flat(n) for n in known}
    exact = [n for n, f in flat.items() if f == key]
    if exact:
        return exact[0], known[exact[0]]
    words = [_flat(w) for w in re.findall(r"[a-z0-9]+", name.lower())
             if w not in ("the", "my", "repo", "project", "folder", "app")]
    hits = [n for n, f in flat.items() if words and all(w in f for w in words)]
    if hits:
        best = min(hits, key=lambda n: len(flat[n]))
        return best, known[best]
    import difflib
    close = difflib.get_close_matches(key, list(flat.values()), n=1, cutoff=0.6)
    if close:
        best = next(n for n, f in flat.items() if f == close[0])
        return best, known[best]
    return None


def pretty(name):
    return re.sub(r"[-_]+", " ", name or "")


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


def running():
    """Jobs whose Claude Code process is still going."""
    return [j for j in list(_jobs.values()) if j["proc"].poll() is None]


def busy(project=None):
    return any(j["project"] == project or project is None for j in running())


def _code_panel(name, path):
    """The project's Claude Code panel on the Stage (reused across runs)."""
    for p in stage.panels():
        if p["kind"] == "code" and p["data"].get("path") == path:
            return p["id"]
    return stage.add("code", f"Claude Code — {name}", {"project": name, "path": path, "status": "idle",
                                                        "events": [], "files": [], "runs": 0})


# ── Git: a branch per task ────────────────────────────────────────────────────
def _git(path, *args, timeout=30):
    """Run git in `path`. Returns (exit code, output)."""
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _is_git(path):
    return os.path.exists(os.path.join(path, ".git"))


def _branch_slug(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    stop = {"a", "an", "the", "to", "for", "of", "and", "in", "on", "with", "my", "me", "please", "can", "you",
            "could", "it", "this", "that", "some", "add", "make", "let's", "lets", "i", "want", "would", "like"}
    picked = [w for w in words if w not in stop][:4] or ["task"]
    return "-".join(picked)[:40].strip("-")


def _prepare_branch(name, path, request, fresh=False):
    """Put the repo on a branch for this task. Follow-ups stay on the jarvis/ branch they're on;
    otherwise a new jarvis/<task> branch is created from the current one. Uncommitted changes
    are asked about first. Returns (go ahead?, note for Claude Code)."""
    code, branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:              # no commits yet: nothing to branch from
        return True, ""
    keep = ("Commit your work there with clear messages (git add and git commit are allowed). Don't push, "
            "merge or switch branches unless the user asks — pushing needs their spoken OK.")
    if branch.startswith("jarvis/") and not fresh:
        return True, f"You're on the branch {branch}, made for earlier Jarvis work in this repo. {keep}"
    _, dirty = _git(path, "status", "--porcelain")
    if dirty:
        from .agent import confirm
        if not confirm(f"{pretty(name)} has uncommitted changes on {branch}. They'd come along to the new branch"):
            speak("Okay, I won't start it.")
            return False, ""
    _, existing = _git(path, "branch", "--list", "jarvis/*", "--format=%(refname:short)")
    taken = set(existing.split())
    new = base = f"jarvis/{_branch_slug(request)}"
    n = 2
    while new in taken:
        new, n = f"{base}-{n}", n + 1
    code, out = _git(path, "checkout", "-b", new)
    if code != 0:
        print(f"  git checkout -b {new} failed: {out}")
        speak(f"I couldn't create a branch in {pretty(name)}, so I didn't start.")
        return False, ""
    print(f"  {name}: working on new branch {new} (from {branch})")
    return True, f"Jarvis created the branch {new} from {branch} for this task. {keep}"


def start(request, project=None, new=False, effort="", model="", plan=False, extra_allowed=(), quiet=False,
          fresh_branch=False, question=False):
    """Send a request to Claude Code in a project (in the background). Returns a status string."""
    exe = claude_exe()
    if not exe:
        speak("Claude Code isn't installed. Install it with npm install -g @anthropic-ai/claude-code.")
        return "Claude Code not installed"
    active = running()
    if len(active) >= config.CODE_MAX_PARALLEL:
        speak(f"{len(active)} projects are already being worked on: "
              + ", ".join(pretty(j["project"]) for j in active)
              + ". Wait for one to finish, or tell me to stop one.")
        return "Too many runs"

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
    if busy(name):
        speak(f"Claude Code is still working on {pretty(name)}. Wait until it's done, "
              "or tell me to stop it.")
        return "Busy"
    _close_remote(name)

    # A task in a git repo gets its own branch; a plan changes nothing, so it waits for the build
    branch_note = ""
    if config.CODE_BRANCH_PER_TASK and not new and not plan and not question and _is_git(proj["path"]):
        task = proj.get("plan_request") if request.startswith("Implement the plan") else request
        ok, branch_note = _prepare_branch(name, proj["path"], task or request, fresh=fresh_branch)
        if not ok:
            return "Cancelled"
    with _lock:
        entry = _state["projects"].get(name)
        if entry is not None and plan:
            entry["plan_request"] = request

    effort = effort if effort in _EFFORTS else config.CODE_EFFORT
    model = model if (model in _MODELS or model.startswith("claude-")) else config.CODE_MODEL
    # a question about the code or its GitHub issues/PRs runs read-only (plan mode), quick by default
    mode = "plan" if plan or question else config.CODE_PERMISSION_MODE
    if question and not effort:
        effort = "low"
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
    if branch_note:
        prompt += f"\n\n{branch_note}"
    if question:
        prompt += "\n\nThis is a question: just answer it. Don't propose a plan or change anything."
    if not new and _is_git(proj["path"]):
        code, remote = _git(proj["path"], "remote", "get-url", "origin")
        m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote) if code == 0 else None
        if m:
            prompt += f"\n\nThis repo's GitHub remote is {m.group(1)}/{m.group(2)} (use it for GitHub tools)."

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
    job = {"proc": proc, "project": name, "panel": pid, "request": request, "events": events,
           "files": list(p["data"].get("files", [])), "dirty": False, "plan": plan, "quiet": quiet,
           "commands": {}, "succeeded": set()}
    _jobs[name] = job
    threading.Thread(target=_read_output, args=(job,), daemon=True, name=f"claude-code-{name}").start()
    threading.Thread(target=_flush_loop, args=(job,), daemon=True).start()
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
    stopped = job.get("stopped")
    status = "stopped" if stopped else "error" if (not result or result.get("is_error")) else \
             ("planned" if job["plan"] else "done")
    summary = (result or {}).get("result", "") or ("Stopped." if stopped else "Claude Code stopped unexpectedly.")
    summary = re.sub(r"^\s*(all )?done[.!]\s*", "", summary, flags=re.IGNORECASE) or summary
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
        if _jobs.get(job["project"]) is job:
            del _jobs[job["project"]]

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
    _notify_finish(job, status, summary, denials)
    who = pretty(job["project"])
    if status == "error":
        speak(f"Claude Code ran into a problem in {who}. The details are on the stage.")
    elif status == "planned":
        speak(f"The plan for {who} is ready on the stage. Shall I go ahead and build it, or is there anything to change?")
    else:
        speak(f"{who[:1].upper() + who[1:]} is done. {_speakable(summary)}")
    if denials:
        first = _speakable_denial(denials[0])
        more = f" and {len(denials) - 1} other command{'s' if len(denials) > 2 else ''}" if len(denials) > 1 else ""
        speak(f"In {who}, it also wanted to run {first}{more}, which needs your permission — the commands "
              "are on the stage. Should I let it?")


def use_path(path):
    """Make an existing folder (anywhere) the current project."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return None
    name = os.path.basename(path.rstrip("\\/")) or "project"
    _use(name, {"path": path})
    return name


def _speakable_denial(d):
    """What a refused tool call was, in words: "git push", or "create pull request" for a GitHub tool."""
    if d["tool"] and d["tool"] not in ("PowerShell", "Bash"):
        action = d["tool"].split("__")[-1].replace("_", " ")
        return f"{action} on GitHub" if d["tool"].startswith("mcp__github") else action
    return _speakable_command(d["command"])


def _speakable_command(cmd):
    """A refused command, short enough to say: the last part, paths cut down to file names."""
    part = re.split(r"\s*(?:;|&&)\s*", cmd.strip())[-1]
    part = re.sub(r'"?(?:[A-Za-z]:)?[\\/][^"\s]*[\\/]([^"\\/\s]+)"?', r"\1", part)
    return part[:80]


def stop(project=None):
    """Stop a run: the named project's, else the current project's, else the only one running.
    Returns the names of the projects stopped."""
    active = {j["project"]: j for j in running()}
    if project:
        hit = _find_project(project)
        targets = [hit[0]] if hit and hit[0] in active else []
    elif current()[0] in active:
        targets = [current()[0]]
    else:
        targets = list(active) if len(active) == 1 else []
    for name in targets:
        active[name]["stopped"] = True
        subprocess.run(["taskkill", "/PID", str(active[name]["proc"].pid), "/T", "/F"], capture_output=True)
    return targets


def approve(project=None):
    """Re-run with the commands Claude Code was refused, now allowed: in the named project,
    else the current one, else whichever project asked most recently."""
    with _lock:
        waiting = {n: p for n, p in _state["projects"].items() if p.get("pending")}
    hit = _find_project(project) if project else None
    if hit and hit[0] in waiting:
        name = hit[0]
    elif current()[0] in waiting:
        name = current()[0]
    else:
        name = max(waiting, key=lambda n: waiting[n].get("last", 0), default=None)
    if not name:
        speak("Nothing is waiting for permission.")
        return "Nothing pending"
    pending = waiting[name]["pending"]
    # Claude Code checks each part of a combined command ("cd x; python y") separately,
    # so allow every part — by its program and first argument — not the whole string
    allowed = []
    for d in pending:
        tool = d["tool"] or "PowerShell"
        if tool not in ("PowerShell", "Bash"):      # an MCP or other tool: allow the tool itself
            allowed.append(tool)
            continue
        for part in re.split(r"\s*(?:;|&&|\|\||\|)\s*", d["command"]):
            words = part.strip().split()
            if words:
                prefix = " ".join(words[:2]) if len(words) > 1 and not words[1].startswith(("-", '"', "'")) else words[0]
                allowed.append(f"{tool}({prefix}:*)")
    cmds = "\n".join(f"- {d['command']}" for d in pending)
    speak("Okay, letting it run those.")
    return start(f"The user approved these commands that were refused before:\n{cmds}\n"
                 "Run them now and finish the task.", project=name, extra_allowed=allowed)


def _project_or_current(project=None):
    with _lock:
        if project in _state["projects"]:
            return project, _state["projects"][project]
    return current()


def open_vscode(project=None):
    name, proj = _project_or_current(project)
    if not proj:
        return False
    subprocess.Popen(["code", proj["path"]], shell=True)
    return True


def open_terminal(project=None):
    """Take over in an interactive Claude Code window, continuing the same conversation."""
    name, proj = _project_or_current(project)
    exe = claude_exe()
    if not proj or not exe:
        return False
    resume = f" --resume {proj['session']}" if proj.get("session") else ""
    q = proj["path"].replace("'", "''")
    subprocess.Popen(["powershell", "-NoExit", "-Command",
                      f"Set-Location -LiteralPath '{q}'; & '{exe}'{resume}"],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True


# ── Continuing on the phone (Remote Control) ──────────────────────────────────
_remote: dict = {}      # project name -> the minimized window hosting its Remote Control session


def trusted(path):
    """Whether Claude Code's "Do you trust the files in this folder?" prompt was accepted for
    `path`. Interactive sessions stop at that prompt (headless runs skip it)."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude.json"), encoding="utf-8") as f:
            projects = json.load(f).get("projects", {})
    except (OSError, ValueError):
        return False
    want = os.path.normcase(os.path.abspath(path))
    folders = {os.path.normcase(os.path.abspath(k)): v for k, v in projects.items()}
    while True:      # trusting a parent folder covers everything inside it
        if folders.get(want, {}).get("hasTrustDialogAccepted"):
            return True
        parent = os.path.dirname(want)
        if parent == want:
            return False
        want = parent


def open_on_phone(project=None):
    """Reopen a project's Claude Code conversation interactively with Remote Control on, in a
    minimized window, so it shows up in the Claude app (and claude.ai/code) with its full
    history. Headless runs can't be watched live; this hands the conversation over once
    Jarvis's run is done. A folder Claude Code hasn't been trusted in yet opens visibly
    instead, so you can accept that prompt once. Returns (project name or None, trusted?)."""
    name, proj = _project_or_current(project)
    exe = claude_exe()
    if not proj or not exe or not proj.get("session") or busy(name):
        return None, True
    _close_remote(name)
    ok = trusted(proj["path"])
    q = proj["path"].replace("'", "''")
    label = f"Jarvis - {pretty(name)}".replace("'", "")
    info = subprocess.STARTUPINFO()
    if ok:
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 7      # SW_SHOWMINNOACTIVE: minimized, doesn't steal focus
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}
    if not config.CODE_USE_API_KEY:
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    _remote[name] = subprocess.Popen(
        ["powershell", "-NoExit", "-Command",
         f"$host.UI.RawUI.WindowTitle = '{label}'; Set-Location -LiteralPath '{q}'; "
         f"& '{exe}' --resume {proj['session']} --remote-control '{label}'"],
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=info, env=env)
    print(f"  {name}: Remote Control session opened ({label})" + ("" if ok else " — waiting for the folder trust prompt"))
    return name, ok


def _close_remote(name):
    """Jarvis is about to run in this project: close its phone session so the two don't both
    write to the same conversation."""
    proc = _remote.pop(name, None)
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        print(f"  {name}: closed its Remote Control session")
        return True
    return False


def _notify_finish(job, status, summary, denials):
    """Away from the PC (or CODE_NOTIFY = "always"): tell the phone a run finished, with buttons
    to continue it in the Claude app and to allow what was refused."""
    from . import notify
    if job["quiet"] or config.CODE_NOTIFY == "never" or (config.CODE_NOTIFY == "away" and not notify.away()):
        return
    who = pretty(job["project"])
    title = {"error": f"{who} hit a problem", "planned": f"Plan ready: {who}"}.get(status, f"{who} is done")
    text = _speakable(summary, limit=900)
    if denials:
        title = f"{who} needs your OK"
        text += "\n\nWants to run: " + "; ".join(_speakable_denial(d) for d in denials[:3])
    buttons = [notify.button("Continue on phone", {"do": "phone", "project": job["project"]})]
    if denials:
        buttons.append(notify.button("Allow", {"do": "approve", "project": job["project"]}))
    notify.send(title, text, tags=["computer"], priority=4 if denials else 3,
                actions=[b for b in buttons if b])


# ── Repos ─────────────────────────────────────────────────────────────────────
def _github_repos():
    """Your GitHub repos: [{"name", "clone_url", "private"}] (private ones need GITHUB_TOKEN)."""
    import requests
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "JARVIS"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
        url, params = "https://api.github.com/user/repos", {"per_page": 100, "affiliation": "owner"}
    else:
        url, params = f"https://api.github.com/users/{config.GITHUB_USER}/repos", {"per_page": 100}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        return [{"name": x["name"], "clone_url": x["clone_url"], "private": x.get("private", False)} for x in r.json()]
    except Exception as e:
        print(f"  GitHub repo list failed: {e}")
        return []


def clone(name):
    """Clone one of your GitHub repos into REPO_DIRS[0] (or switch to it if it's already here).
    Returns (project name, message)."""
    local = _find_project(name)
    if local and _flat(local[0]) == _flat(name):
        _use(*local)
        return local[0], f"{pretty(local[0])} is already here. It's now the current project."
    repos = _github_repos()
    flat = {_flat(r["name"]): r for r in repos}
    import difflib
    key = _flat(name)
    pick = flat.get(key) or next((r for f, r in flat.items() if key and key in f), None)
    if not pick:
        close = difflib.get_close_matches(key, list(flat), n=1, cutoff=0.6)
        pick = flat[close[0]] if close else None
    if not pick:
        return None, f"I couldn't find a repo called {name} on your GitHub."
    dest = os.path.join(config.REPO_DIRS[0], pick["name"])
    if os.path.isdir(dest):
        _use(pick["name"], {"path": dest})
        return pick["name"], f"{pretty(pick['name'])} is already cloned. It's now the current project."
    speak(f"Cloning {pretty(pick['name'])}.")
    code, out = _git(config.REPO_DIRS[0], "clone", pick["clone_url"], dest, timeout=300)
    if code != 0:
        print(f"  git clone failed: {out}")
        return None, f"Cloning {pretty(pick['name'])} failed. The details are in the console."
    _use(pick["name"], {"path": dest})
    return pick["name"], f"Cloned {pretty(pick['name'])}. It's now the current project."


def repo_status():
    """Branch and uncommitted changes for every git repo. Shows a note on the Stage; returns speech."""
    rows, dirty, jarvis = [], [], []
    for name in repo_names():
        path = next(os.path.join(r, name) for r in config.REPO_DIRS if os.path.isdir(os.path.join(r, name)))
        if not _is_git(path):
            continue
        _, branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        _, changes = _git(path, "status", "--porcelain")
        n = len([line for line in changes.splitlines() if line.strip()])
        if n:
            dirty.append(name)
        if branch.startswith("jarvis/"):
            jarvis.append(name)
        if n or branch.startswith("jarvis/") or busy(name):
            state = "working now" if busy(name) else f"{n} uncommitted change{'s' if n != 1 else ''}" if n else "clean"
            rows.append(f"| {name} | `{branch}` | {state} |")
    md = ("Repos with uncommitted changes, Jarvis branches or a run in progress. Everything else is clean.\n\n"
          "| Repo | Branch | State |\n|---|---|---|\n" + "\n".join(rows)) if rows else "Every repo is clean."
    pid = next((p["id"] for p in stage.panels() if p["kind"] == "note" and stage.private(p["id"]).get("repos")), None)
    if pid:
        stage.update(pid, markdown=md)
        stage.navigate("/stage")
    else:
        stage.add("note", "Repo status", {"markdown": md}, private={"repos": True})
    parts = []
    if dirty:
        parts.append(f"{len(dirty)} repo{'s have' if len(dirty) != 1 else ' has'} uncommitted changes: "
                     + ", ".join(pretty(n) for n in dirty[:5]) + ("" if len(dirty) <= 5 else ", and more"))
    if jarvis:
        parts.append(f"{len(jarvis)} {'are' if len(jarvis) != 1 else 'is'} on a Jarvis branch: "
                     + ", ".join(pretty(n) for n in jarvis[:5]))
    return (". ".join(parts) + ".") if parts else "All your repos are clean."


# ── Voice ─────────────────────────────────────────────────────────────────────
@action("code",
        schema='{"type":"code","command":"new|run|switch|list|clone|repos|status|stop|approve|phone|remember|show_prefs|open_vscode|open_terminal","request":"<the full coding request, in the user\'s words>","project":"<project or repo name, or empty for the current one>","effort":"<low|medium|high|xhigh|max, or empty>","model":"<opus|sonnet|fable|haiku, or empty>","plan_only":false,"question":false,"fresh_branch":false,"preference":"<for remember>"}',
        rules=['"code/build/make/write me a [app, script, game, project]", "start a new project for X" → code command "new"; "request" = the full description; "project" = a short name for it',
               'ANY change to the current coding project ("add dark mode", "fix the login bug", "write tests", "refactor X", "why does it crash") → code command "run" — it goes straight to Claude Code. Keep the user\'s wording in "request", including things like "use subagents" or "in parallel". This is for project code; files Jarvis wrote to the Stage use edit_file.',
               'Thinking depth: "think hard/harder" → effort "high"; "think really hard", "ultrathink", "max effort", "as thorough as possible" → "max"; "quick", "low effort" → "low". "use Opus/Sonnet/Haiku" → model. Leave both empty otherwise.',
               '"plan it first", "just plan", "don\'t change anything yet" → plan_only true. After a plan, "go ahead", "build it", "do it" → code command "run" with request "Implement the plan you proposed."',
               '"switch to / work on [project or repo]" → code "switch" with project (any folder in Repos counts); a request naming another repo ("in murphys, add a footer") → code "run" with that project. "what projects/repos do I have" → "list"; "how\'s the coding going", "what is Claude Code doing" → "status"; "stop coding", "stop the murphys run" → "stop" (project if named)',
               'Questions about a repo that change nothing — "any new issues on hiclimb", "what PRs are open", "is CI passing", "how does the login work in X" → code "run" with question true, the project, and the question as request',
               '"clone my X repo", "get X from GitHub" → code "clone" with project; "status of my repos", "which repos have changes" → code "repos"',
               'Work in a git repo happens on its own jarvis/ branch automatically, and follow-ups continue on it. "start a fresh branch", "new branch for this" → fresh_branch true. Pushing, merging into main and opening PRs are requests to Claude Code (code "run"); Jarvis asks before anything is pushed.',
               '"allow it", "yes run it", "yes" (after Jarvis asked permission for a command) → code "approve"',
               'Coding preferences only — "remember I prefer X for coding", "always use X in my projects", "from now on use X when you code" → code "remember" with preference = the preference as a short sentence (anything else to remember is type "memory"); "show my coding preferences" → "show_prefs"',
               '"continue this on my phone", "put it on my phone", "let me keep going from my phone" → code "phone" (project if named) — opens the conversation in the Claude app with Remote Control',
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
        elif a.get("question"):
            speak("Let me check.")
        else:
            speak("Handing that to Claude Code.")
        return start(request, project=a.get("project") or None, new=(cmd == "new"),
                     effort=(a.get("effort") or "").lower(), model=(a.get("model") or "").lower(),
                     plan=bool(a.get("plan_only")), fresh_branch=bool(a.get("fresh_branch")),
                     question=bool(a.get("question")))
    if cmd == "switch":
        hit = _find_project(a.get("project", ""))
        if not hit:
            speak(f"I couldn't find a project called {a.get('project', '')}.")
            return "Code: project not found"
        _use(*hit)
        branch = _git(hit[1]["path"], "rev-parse", "--abbrev-ref", "HEAD")[1] if _is_git(hit[1]["path"]) else ""
        speak(f"Now working on {pretty(hit[0])}" + (f", on branch {branch.replace('/', ' ')}." if branch else "."))
        return f"Current project: {hit[0]}"
    if cmd == "list":
        repos = repo_names()
        mine = [n for n, p in _state["projects"].items()
                if not any(os.path.normcase(p["path"]).startswith(os.path.normcase(r)) for r in config.REPO_DIRS)]
        parts = []
        if repos:
            parts.append(f"You have {len(repos)} repos in your GitHub folder, including "
                         + ", ".join(pretty(n) for n in repos[:6]) + ". The full list is on the stage.")
            stage.add("note", "Repos", {"markdown": "\n".join(f"- {n}" for n in repos)})
        if mine:
            parts.append("Projects I built: " + ", ".join(pretty(n) for n in mine[:8]) + ".")
        speak(" ".join(parts) or "You don't have any coding projects yet.")
        return "Code: listed"
    if cmd == "clone":
        name, msg = clone(a.get("project") or request)
        speak(msg)
        return f"Code: clone {name}"
    if cmd == "repos":
        speak(repo_status())
        return "Code: repo status"
    if cmd == "status":
        name, proj = current()
        active = running()
        for job in active:
            last = next((e["text"] for e in reversed(job["events"]) if e["kind"] in ("edit", "command", "text")), "")
            speak(f"{pretty(job['project'])}: still working. Latest: {last[:140]}")
        if not active:
            speak(f"Claude Code is idle. The current project is {pretty(name)}." if name
                  else "There's no coding project yet.")
        return "Code: status"
    if cmd == "stop":
        stopped = stop(a.get("project") or None)
        if stopped:
            speak("Stopping Claude Code in " + ", ".join(pretty(n) for n in stopped) + ".")
        elif len(running()) > 1:
            speak("Several projects are running: " + ", ".join(pretty(j["project"]) for j in running())
                  + ". Which one should I stop?")
        else:
            speak("Claude Code isn't running.")
        return "Code: stop"
    if cmd == "approve":
        return approve(a.get("project") or None)
    if cmd == "phone":
        hit = _find_project(a.get("project")) if a.get("project") else None
        target = hit[0] if hit else None
        if busy(target or current()[0]):
            speak("Claude Code is still working on it. I'll be able to hand it over once it's done.")
            return "Code: phone (busy)"
        name, ok = open_on_phone(target)
        if not name:
            speak("There's no conversation to hand over yet. Give it a task first.")
        elif not ok:
            speak(f"Claude Code hasn't been trusted in {pretty(name)} yet. Accept the prompt in the window "
                  f"I just opened, then it'll show up in the Claude app as Jarvis {pretty(name)}.")
        else:
            speak(f"{pretty(name)[:1].upper() + pretty(name)[1:]} is open in the Claude app on your phone, "
                  f"under Jarvis {pretty(name)}.")
        return f"Code: phone {name}"
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
