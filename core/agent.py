"""
General-purpose fallback: when no built-in action fits, Claude works through the
task with a small set of tools (PowerShell, files, web, Claude Code hand-off).

Safety: read-only PowerShell runs immediately; anything that could change the
system — and any overwrite of an existing file — is described out loud and only
runs after a spoken "yes".
"""

import os
import re
import subprocess
import webbrowser

from . import audio, brain, config, verbosity
from .tts import speak, wait_tts
from .web import brave_search, fetch_text

MAX_STEPS = 8
_HOME     = os.path.expanduser("~")

_SYSTEM = f"""You are JARVIS, a voice assistant that controls the user's Windows 11 PC.
Complete the task with the tools, in as few steps as possible.
- Shell is Windows PowerShell 5.1: no "&&", no "??". User home: {_HOME}. Desktop: {os.path.join(_HOME, "OneDrive", "Desktop")}.
- Look before you change things (list/read first) and never guess paths.
- For writing or fixing code in a project, use delegate_to_claude_code instead of editing code yourself. It runs in the background and reports back by itself, so finish right after starting it.
- If the user declines a confirmation, stop and say so.
- When done, say the outcome in plain spoken sentences. No markdown, no lists.
- Length: """

TOOLS = [
    {
        "name": "run_powershell",
        "description": "Run a Windows PowerShell 5.1 command and return its output (truncated to 4000 chars). "
                       "Read-only Get-/Test-/Select- style commands run immediately; anything else is read "
                       "aloud to the user for spoken confirmation first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "summary": {"type": "string", "description": "Short plain-English description of what the command does, spoken to the user if confirmation is needed."},
            },
            "required": ["command", "summary"],
        },
    },
    {
        "name": "list_directory",
        "description": "List the files and folders in a directory.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "read_file",
        "description": "Read a text file (first 8000 characters).",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file. Overwriting an existing file asks the user first.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web; returns titles, snippets and URLs.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "fetch_url",
        "description": "Download a web page and return its text.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "open",
        "description": "Open a URL in the browser, or a file/folder with its default app.",
        "input_schema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
    },
    {
        "name": "delegate_to_claude_code",
        "description": "Hand a coding task to Claude Code (an AI coding agent), working in the given project "
                       "directory. It runs in the background; Jarvis announces the result when it finishes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task":      {"type": "string", "description": "Complete, self-contained instructions."},
                "directory": {"type": "string", "description": "Project folder. Defaults to the jarvis_coding folder."},
            },
            "required": ["task"],
        },
    },
]


# ── Confirmation ──────────────────────────────────────────────────────────────
_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|go ahead|do it|okay|ok|confirm|please)\b", re.IGNORECASE)
_NO  = re.compile(r"\b(no|nope|don't|do not|stop|cancel|wait)\b", re.IGNORECASE)


def confirm(summary) -> bool:
    speak(f"{summary}. Should I go ahead?")
    wait_tts()
    answer = audio.listen_for_command(max_duration=6, silence_duration=0.8)
    approved = bool(_YES.search(answer)) and not _NO.search(answer)
    print(f"  Confirmation {'granted' if approved else 'declined'}: {answer!r}")
    return approved


# Verbs whose cmdlets only read state. Every Verb-Noun token in the command must use one.
_SAFE_VERBS = {"get", "test", "select", "where", "sort", "measure", "format",
               "resolve", "convertto", "group", "compare"}


def _is_read_only(cmd: str) -> bool:
    if re.search(r"[;&>`]|\$\(|\binvoke\b|\biex\b", cmd, re.IGNORECASE):
        return False
    for segment in cmd.split("|"):
        first = segment.strip().split(" ", 1)[0].lower()
        if "-" not in first or first == "out-file":
            return False
    for verb, noun in re.findall(r"\b([A-Za-z]+)-([A-Za-z]+)\b", cmd):
        v = verb.lower()
        if v == "out" and noun.lower() == "string":
            continue
        if v not in _SAFE_VERBS:
            return False
    return True


# ── Tools ─────────────────────────────────────────────────────────────────────
def _run_powershell(command, summary):
    if not _is_read_only(command) and not confirm(summary or "I'd like to run a PowerShell command"):
        return "User declined. Do not retry; tell the user it was cancelled.", True
    print(f"  [agent] PS> {command}")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout + ("\n[stderr]\n" + r.stderr if r.stderr.strip() else "")).strip()
    return (out or f"(no output, exit code {r.returncode})")[:4000], r.returncode != 0


def _write_file(path, content):
    path = os.path.expanduser(path)
    if os.path.exists(path) and not confirm(f"I'd like to overwrite {os.path.basename(path)}"):
        return "User declined the overwrite.", True
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {len(content)} characters to {path}", False


def _delegate(task, directory=""):
    """Background Claude Code run via core/coding.py (progress on the Stage, spoken result)."""
    from . import coding
    if directory:
        if not coding.use_path(directory):
            return f"Directory not found: {directory}", True
    elif not coding.current()[0]:
        return "No current coding project. Ask the user which project, or start one with the code action.", True
    status = coding.start(task, quiet=False)
    return f"{status}. It reports back by itself when done.", status.startswith(("Busy", "Claude Code not", "No current"))


def _run_tool(name, args):
    try:
        if name == "run_powershell":
            return _run_powershell(args.get("command", ""), args.get("summary", ""))
        if name == "list_directory":
            path = os.path.expanduser(args["path"])
            entries = sorted(os.listdir(path))
            return "\n".join(e + ("/" if os.path.isdir(os.path.join(path, e)) else "") for e in entries)[:4000] or "(empty)", False
        if name == "read_file":
            with open(os.path.expanduser(args["path"]), encoding="utf-8", errors="ignore") as f:
                return f.read(8000), False
        if name == "write_file":
            return _write_file(args["path"], args.get("content", ""))
        if name == "web_search":
            results = brave_search(args["query"])
            if not results:
                return "No results (or BRAVE_API_KEY is not set).", True
            return "\n".join(f"{r['title']}: {r['description']} ({r['url']})" for r in results), False
        if name == "fetch_url":
            return fetch_text(args["url"]), False
        if name == "open":
            target = args["target"]
            if target.startswith("http"):
                webbrowser.open(target)
            else:
                os.startfile(os.path.expanduser(target))
            return f"Opened {target}", False
        if name == "delegate_to_claude_code":
            return _delegate(args["task"], args.get("directory", ""))
        return f"Unknown tool: {name}", True
    except Exception as e:
        return f"{type(e).__name__}: {e}", True


# ── Loop ──────────────────────────────────────────────────────────────────────
def run_agent(task, context="") -> str:
    content = f"{task}\n\nContext from earlier steps:\n{context[:4000]}" if context else task
    messages = [{"role": "user", "content": [{"type": "text", "text": content}]}]
    marked = None
    for _ in range(MAX_STEPS):
        # Each step resends everything so far; mark the newest block so the next step
        # reads all of it from the cache (only the latest mark is kept — the API allows 4)
        if marked is not None:
            marked.pop("cache_control", None)
        marked = messages[-1]["content"][-1]
        marked["cache_control"] = config.CACHE_CONTROL
        response = config.client.messages.create(
            model=config.MODEL,
            max_tokens=1500,
            system=_SYSTEM + verbosity.rule(),
            tools=TOOLS,
            messages=messages,
        )
        brain.log_usage("agent", response.usage)
        if response.stop_reason != "tool_use":
            return " ".join(b.text for b in response.content if b.type == "text").strip() or "Done."
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output, is_error = _run_tool(block.name, block.input)
                print(f"  [agent] {block.name} -> {output[:120]!r}")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "I couldn't finish that within my step limit."
