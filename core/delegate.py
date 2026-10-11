"""
Delegated tasks: open-ended work that needs an outside service — "find the email from Sam
and draft a reply", "find me software internships in Seattle that fit my resume", "make a
Miro mind map of this plan" — goes to Claude Code, billed to your Claude subscription
instead of the API.

Each service runs with only its own MCP servers (--mcp-config <file> --strict-mcp-config),
so a Miro task never starts the job scraper and nothing else's tool list is loaded. Google
is the exception: it uses your claude.ai Gmail / Drive / Calendar connectors (no Google
Cloud setup), which load with your normal Claude Code setup; which of their tools run
without asking is decided once by scripts/setup_mcp.py google (google_tools.json).
Tools that only read (or make something new) are allowed up front; anything that sends,
deletes or shares is refused and Jarvis asks you ("allow it"), exactly like a refused
command in a coding run — coding.py runs everything (Stage panel, approval, phone).

A service keeps its conversation for follow-ups ("reply to the second one") until it's
been idle DELEGATE_SESSION_HOURS. Long results (a list of jobs, a table) are written to
result.md and shown on the Stage; only a short summary is spoken.

The config files written to jarvis_memory/mcp/ hold no secrets: keys are ${VAR}
references that Claude Code fills in from the environment (.env).
"""

import json
import os
import sys

from . import config
from .registry import action
from .tts import speak

_MCP_DIR = os.path.join(config.MEMORY_DIR, "mcp")
GOOGLE_TOOLS = os.path.join(_MCP_DIR, "google_tools.json")   # written by scripts/setup_mcp.py google
_CONNECTORS = ("mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Drive", "mcp__claude_ai_Google_Calendar")
# Connector tools whose names say they send, delete, share or change something are asked about
_ASK = r"send|delete|trash|remove|update|modify|edit|share|permission|move|label|archive|respond|rsvp"


def classify(tools):
    """Split connector tool names into (allowed up front, asked about first)."""
    import re
    ask = [t for t in tools if re.search(_ASK, t.split("__")[-1], re.IGNORECASE)]
    return [t for t in tools if t not in ask], ask


def google_tools():
    """{"allowed": [...], "ask": [...]} from the setup script, or None when the connectors
    haven't been checked yet."""
    try:
        with open(GOOGLE_TOOLS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def google_ready():
    tools = google_tools()
    return bool(tools and any(t.startswith("mcp__claude_ai_Gmail") for t in tools.get("allowed", [])))


def _google_allowed():
    return (google_tools() or {}).get("allowed", [])


def resume_file():
    """The resume for job searches: resume.md or resume.txt as is, or resume.pdf turned into
    plain text (resume.pdf.txt, redone when the PDF changes). Text is several times cheaper for
    Claude Code to read than a PDF, which it also reads as page images. "" when there's none."""
    base = config.RESUME_FILE
    for ext in (".md", ".txt"):
        if os.path.isfile(base + ext):
            return base + ext
    pdf, txt = base + ".pdf", base + ".pdf.txt"
    if not os.path.isfile(pdf):
        return ""
    if os.path.isfile(txt) and os.path.getmtime(txt) >= os.path.getmtime(pdf):
        return txt
    try:
        from pypdf import PdfReader
        text = "\n\n".join((page.extract_text() or "").strip() for page in PdfReader(pdf).pages).strip()
    except Exception as e:
        print(f"  Couldn't read {pdf} as text ({e}); job searches will read the PDF itself")
        return pdf
    if len(text) < 200:          # a scanned PDF has no text layer
        return pdf
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return txt


def uvx_exe():
    """uvx on PATH, else in Python's per-user Scripts folder (where `pip install uv` often puts it)."""
    import shutil
    import sysconfig
    found = shutil.which("uvx")
    if found:
        return found
    user = os.path.join(sysconfig.get_path("scripts", f"{os.name}_user"), "uvx.exe" if os.name == "nt" else "uvx")
    return user if os.path.isfile(user) else ""


def _uvx(*args):
    # uvx brings its own Python: these servers don't support 3.14 yet
    return {"command": uvx_exe() or "uvx", "args": ["--python", "3.12", *args]}


def _resume_note():
    path = resume_file()
    if path:
        return (f"The user's resume is at {path} — read it first and rank results by how well they fit it "
                "(skills, level, field, graduation date), saying why in a few words each. ")
    return "There's no resume on file yet, so filter on what the user asked for. "


def _recent_files():
    """Files Jarvis made lately, so "save that to my Drive" knows what "that" is."""
    out = config.JARVIS_OUTPUT_DIR
    files = [os.path.join(out, f) for f in os.listdir(out)] if os.path.isdir(out) else []
    files = [f for f in files if os.path.isfile(f) and not f.endswith("jarvis_matches.txt")]
    return sorted(files, key=os.path.getmtime, reverse=True)[:3]


# name -> what it can do, its MCP servers, and the tools allowed without asking.
#   servers: its own MCP servers (dict, or a function returning one; empty = none)
#   connectors: also load the claude.ai connectors and your user-scope Claude Code servers
SERVICES = {
    "google": {
        "label": "Google",
        "about": "Gmail, Google Drive, Google Calendar",
        "servers": {},
        "connectors": True,         # the claude.ai Gmail / Drive / Calendar connectors
        "allowed": _google_allowed,
        "note": lambda: ("You have the user's Gmail, Google Drive and Google Calendar through the claude.ai "
                         "connectors. Never send an email unless the user explicitly asked to send it — draft it "
                         "instead and say it's in Drafts. Only change existing files or events the user named. "
                         "Put links to anything you created in result.md. "
                         + ("Files Jarvis made recently (newest first; \"that file\" means the first): "
                            + "; ".join(_recent_files()) + ". " if _recent_files() else "")),
    },
    "jobs": {
        "label": "Job search",
        "about": "job and internship listings from LinkedIn, Indeed, Glassdoor, ZipRecruiter and Google Jobs",
        "servers": {"jobspy": _uvx("jobspy-mcp")},
        "allowed": ["mcp__jobspy", "mcp__claude_ai_Indeed"],
        "connectors": config.JOBS_INDEED_CONNECTOR,
        "note": lambda: ("Search with jobspy's search_jobs: site_name [\"indeed\", \"linkedin\", \"google\"] unless the "
                         "user named sites, results_wanted about 10 (results are big — Indeed always includes the "
                         "full description), job_type \"internship\" for internships, hours_old for recent "
                         "postings (not together with job_type on Indeed: then filter by title yourself), "
                         "fetch_description false. " + _resume_note() +
                         "Write the best matches to result.md as a table: role, company, location, posted, pay "
                         "if listed, why it fits, and the apply link. "),
    },
    "miro": {
        "label": "Miro",
        "about": "Miro boards: diagrams, mind maps, brainstorming, flowcharts",
        "servers": {"miro": {"type": "http", "url": "https://mcp.miro.com/"}},
        "allowed": ["mcp__miro"],
        "note": ("You have the user's Miro account. Build diagrams and brainstorm boards with clear structure "
                 "and readable layout. Make a new board unless the user named one. Put the board link in "
                 "result.md. "),
    },
    "general": {
        "label": "Claude",
        "about": "anything else, with every tool and connector your Claude Code has",
        "servers": {},
        "allowed": [],
        "connectors": True,
        "note": "",
    },
}


def _servers(name):
    svc = SERVICES[name]
    servers = svc["servers"]() if callable(svc["servers"]) else svc["servers"]
    return servers


def config_file(name):
    """Write the service's MCP config (no secrets in it) and return its path, or None when
    the service uses your normal Claude Code setup instead."""
    servers = _servers(name)
    if not servers:
        return None
    os.makedirs(_MCP_DIR, exist_ok=True)
    path = os.path.join(_MCP_DIR, f"{name}.json")
    if sys.platform == "win32":
        for s in servers.values():      # Windows can't start npx directly
            if s.get("command") == "npx":
                s["command"], s["args"] = "cmd", ["/c", "npx", *s.get("args", [])]
    body = json.dumps({"mcpServers": servers}, indent=2)
    try:
        with open(path, encoding="utf-8") as f:
            same = f.read() == body
    except OSError:
        same = False
    if not same:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    return path


def cli_flags(name):
    """Claude Code flags that give a run exactly this service's MCP servers."""
    path = config_file(name)
    if path and not SERVICES[name].get("connectors"):
        return ["--mcp-config", path, "--strict-mcp-config"]
    return ["--mcp-config", path] if path else []


def spec(name):
    """Everything coding.start needs to run a task for this service."""
    svc = SERVICES[name]
    path = os.path.join(config.DELEGATE_DIR, name)
    os.makedirs(path, exist_ok=True)
    note = svc["note"]() if callable(svc["note"]) else svc["note"]
    allowed = list(svc["allowed"]() if callable(svc["allowed"]) else svc["allowed"])
    result = os.path.join(path, "result.md")
    return {
        "service": name, "name": f"jarvis-{name}", "path": path, "label": svc["label"],
        "flags": cli_flags(name), "allowed": allowed, "result_file": result,
        "note": (note + f"If your answer is longer than a few sentences (a list, a table, links), write the "
                 f"full version as Markdown to {result} — Jarvis shows it on screen — and keep your final "
                 "spoken summary short, without mentioning result.md or file names (Jarvis says where the full "
                 "version is). Work files go in the current folder. "),
    }


def run(name, request, effort="", model="", extra_allowed=(), context="", quiet=False, on_done=None):
    from . import coding
    if name not in SERVICES:
        name = "general"
    if name == "google" and not google_ready():
        speak("Google isn't connected yet. Connect Gmail, Drive and Calendar at claude dot A I, settings, "
              "connectors, then run python scripts setup MCP google.")
        return "Delegate: Google not connected"
    if context:
        request += f"\n\nContext Jarvis gathered for this request:\n{context[:6000]}"
    return coding.start(request, effort=effort or config.DELEGATE_EFFORT, model=model or config.DELEGATE_MODEL,
                        extra_allowed=extra_allowed, service=spec(name), quiet=quiet, on_done=on_done)


def prompt_line():
    return "; ".join(f'"{n}" = {s["about"]}' for n, s in SERVICES.items())


@action("delegate",
        schema='{"type":"delegate","service":"google|jobs|miro|general","request":"<the full task, in the user\'s words>","effort":"<low|medium|high|max, or empty>"}',
        rules=['Tasks needing an outside service go to Claude in the background → type "delegate". Services: ' + prompt_line(),
               '"check my email", "find the email from X", "what did X say about Y", "draft/reply to an email", "search my Drive for X", "save that to my Drive", "what\'s on my Google Calendar", "put X on my Google Calendar" → delegate "google". Calendar ONLY when the user says Google Calendar or "my calendar"; reminders, deadlines and to-dos are type "agenda".',
               '"find me internships/jobs", "search for X jobs in Y", "which of these fit my resume" → delegate "jobs"',
               '"make a diagram/mind map/flowchart/brainstorm board", "put this plan on Miro" → delegate "miro"',
               '"ask Claude to X", "have Claude research X and write it up", long multi-step research or writing → delegate "general". Coding in a project is type "code"; quick things on the PC are "agent".',
               'Keep the user\'s whole request in "request", including details from the conversation it refers to ("the second job" → which job). "think hard" → effort "high".'])
def _delegate(a, chain):
    name = a.get("service") or "general"
    request = (a.get("request") or "").strip()
    if not request:
        speak("What should I have it do?")
        return "Delegate: no request"
    speak({"google": "Checking your Google account.", "jobs": "Searching the job boards. I'll tell you what I find.",
           "miro": "Building it in Miro."}.get(name, "Handing that to Claude. I'll let you know when it's done."))
    return run(name, request, effort=(a.get("effort") or "").lower(),
               context=chain.get("file_context") or chain.get("image_context") or "")
