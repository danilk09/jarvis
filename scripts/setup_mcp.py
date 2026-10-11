"""
Set up the MCP servers Jarvis uses. Run one part at a time:

    python scripts/setup_mcp.py status    what's set up and what's missing
    python scripts/setup_mcp.py google    check your claude.ai Gmail / Drive / Calendar connectors and
                                          decide which of their tools run without asking
    python scripts/setup_mcp.py coding    Context7, Playwright, Docker, Postman, Postgres, Firebase
                                          → Claude Code, for every project (user scope)
    python scripts/setup_mcp.py jobs      download the JobSpy server (job and internship search)
    python scripts/setup_mcp.py miro      sign in to Miro (opens Claude Code; type /mcp there)

Google uses the connectors on your Claude account — no Google Cloud project: claude.ai →
Settings → Connectors → connect Gmail, Google Drive and Google Calendar. Then run the google part.

Optional .env values for the coding servers:
    POSTGRES_URL=postgresql://user:password@localhost:5432/dbname   (read-only access)
    POSTMAN_API_KEY=PMAK-...      (without it, Postman signs in through the browser: claude mcp login postman)
    CONTEXT7_API_KEY=...          (free at context7.com — higher rate limits; works without)
"""

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config, delegate  # noqa: E402  (loads .env)

WIN = sys.platform == "win32"


def _claude():
    exe = shutil.which("claude")
    if not exe:
        sys.exit("Claude Code isn't on PATH (npm install -g @anthropic-ai/claude-code).")
    return exe


def _uv():
    """The uvx executable (installing uv if needed), or ""."""
    if delegate.uvx_exe():
        return delegate.uvx_exe()
    print("Installing uv (runs the Python-based MCP servers)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=False)
    if not delegate.uvx_exe():
        print("  uvx wasn't found after installing. Install uv from https://docs.astral.sh/uv/ and run this again.")
    return delegate.uvx_exe()


def _npx(*args):
    return {"command": "cmd", "args": ["/c", "npx", "-y", *args]} if WIN else {"command": "npx", "args": ["-y", *args]}


def _registered():
    """Names of user-scope servers in ~/.claude.json."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude.json"), encoding="utf-8") as f:
            return set(json.load(f).get("mcpServers", {}))
    except (OSError, ValueError):
        return set()


def _add(name, server, secrets=()):
    claude = _claude()
    subprocess.run([claude, "mcp", "remove", name, "-s", "user"], capture_output=True)
    r = subprocess.run([claude, "mcp", "add-json", "-s", "user", name, json.dumps(server)],
                       capture_output=True, text=True)
    out = r.stderr or r.stdout
    for s in secrets:
        out = out.replace(s, "***")
    print(f"  {name}: " + ("added" if r.returncode == 0 else "FAILED\n    " + out.strip()))


# ── Parts ─────────────────────────────────────────────────────────────────────
def google():
    """Ask Claude Code which tools the claude.ai Google connectors have, and save which may run
    without asking (reading, searching, drafting, creating) and which need your OK (sending,
    deleting, changing, sharing)."""
    print("Asking Claude Code which Google connector tools you have (about 20 seconds)...")
    from core import coding
    r = subprocess.run(
        [coding.claude_exe() or _claude(), "-p", "--output-format", "json", "--model", "haiku"],
        input="Use ToolSearch to load every tool from the claude.ai Gmail, claude.ai Google Drive and claude.ai "
              "Google Calendar servers. Don't call any of them. Reply with ONLY a JSON array of their full "
              "tool names (mcp__...), or [] if the servers need authentication.",
        capture_output=True, text=True, encoding="utf-8", env=coding._env(), timeout=300,
        cwd=config.DELEGATE_DIR if os.path.isdir(config.DELEGATE_DIR) else None)
    try:
        text = json.loads(r.stdout).get("result", "")
        tools = json.loads(text[text.index("["):text.rindex("]") + 1])
    except (ValueError, AttributeError):
        sys.exit("Couldn't read Claude Code's answer:\n" + (r.stdout or r.stderr)[-1500:])
    tools = sorted({t for t in tools if isinstance(t, str) and t.startswith(delegate._CONNECTORS)})
    if not any(t.startswith("mcp__claude_ai_Gmail") for t in tools):
        sys.exit("The Gmail connector isn't signed in. Go to claude.ai → Settings → Connectors, connect Gmail "
                 "(and Google Drive and Google Calendar), then run this again.")
    allowed, ask = delegate.classify(tools)
    os.makedirs(os.path.dirname(delegate.GOOGLE_TOOLS), exist_ok=True)
    with open(delegate.GOOGLE_TOOLS, "w", encoding="utf-8") as f:
        json.dump({"allowed": allowed, "ask": ask}, f, indent=2)
    print("  Run without asking:\n    " + "\n    ".join(allowed))
    print("  Need your OK (\"allow it\"):\n    " + ("\n    ".join(ask) or "(none)"))
    print(f"  Saved to {delegate.GOOGLE_TOOLS} — move a tool between the lists there if you like.")
    print("  Try: \"Jarvis, check my email\" or \"give me a breakdown of today\".")


def coding():
    print("Adding coding servers to Claude Code (user scope — every project, including your own sessions):")
    ctx = {"type": "http", "url": "https://mcp.context7.com/mcp"}
    key = os.environ.get("CONTEXT7_API_KEY", "")
    if key:
        ctx["headers"] = {"CONTEXT7_API_KEY": key}
    _add("context7", ctx, [key] if key else ())
    _add("playwright", _npx("@playwright/mcp@latest"))

    postman = {"type": "http", "url": "https://mcp.postman.com/minimal"}
    pkey = os.environ.get("POSTMAN_API_KEY", "")
    if pkey:
        postman["headers"] = {"Authorization": f"Bearer {pkey}"}
    _add("postman", postman, [pkey] if pkey else ())
    if not pkey:
        print("    Sign in once: claude mcp login postman   (or add POSTMAN_API_KEY to .env and rerun)")

    _add("firebase", _npx("firebase-tools@latest", "mcp"))
    print("    Firebase uses your Firebase CLI login: npx firebase-tools login (once)")

    if shutil.which("docker") and subprocess.run(["docker", "mcp", "--help"], capture_output=True).returncode == 0:
        _add("docker", {"command": "docker", "args": ["mcp", "gateway", "run"]})
        print("    Pick the servers it offers in Docker Desktop → MCP Toolkit (Docker Desktop must be running)")
    else:
        print("  docker: skipped — needs Docker Desktop with the MCP Toolkit")

    db = os.environ.get("POSTGRES_URL", "")
    if db and _uv():
        _add("postgres", {**delegate._uvx("postgres-mcp", "--access-mode=restricted"), "env": {"DATABASE_URI": db}}, [db])
    else:
        print("  postgres: skipped — add POSTGRES_URL to .env (and uv) and rerun")
    print("\nCheck them with: claude mcp list")


def jobs():
    uvx = _uv()
    if not uvx:
        return
    print("Downloading the JobSpy server (first time only)...")
    r = subprocess.run([uvx, "--python", "3.12", "jobspy-mcp", "--help"], capture_output=True, text=True, timeout=600)
    print("  ready" if r.returncode == 0 else "  FAILED:\n" + (r.stderr or r.stdout)[-1500:])
    if not delegate.resume_file():
        print(f"  Save your resume as {config.RESUME_FILE}.pdf (or .md / .txt) so results are ranked by fit.")
    if config.JOBS_INDEED_CONNECTOR:
        print("  Indeed connector on: add it at claude.ai → Settings → Connectors → Indeed.")


def miro():
    path = delegate.config_file("miro")
    print("Opening Claude Code with the Miro server. In it, type /mcp, choose miro, then Authenticate;\n"
          "after signing in in the browser, close the window. Jarvis's Miro tasks reuse that login.")
    os.makedirs(os.path.join(config.DELEGATE_DIR, "miro"), exist_ok=True)
    args = [_claude(), "--mcp-config", path, "--strict-mcp-config"]
    if WIN:
        subprocess.Popen(args, cwd=os.path.join(config.DELEGATE_DIR, "miro"), creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.run(args, cwd=os.path.join(config.DELEGATE_DIR, "miro"))


def status():
    have = _registered()
    rows = [
        ("Google (claude.ai connectors)", delegate.google_ready(),
         "connect them at claude.ai -> Settings -> Connectors, then python scripts/setup_mcp.py google"),
        ("uv / uvx (Python MCP servers)", bool(delegate.uvx_exe()), "pip install uv"),
        ("Wolfram|Alpha", bool(config.WOLFRAM_APP_ID), "WOLFRAM_APP_ID in .env (developer.wolframalpha.com)"),
        ("Resume for job search", bool(delegate.resume_file()), config.RESUME_FILE + ".pdf (or .md / .txt)"),
    ]
    rows += [(f"Claude Code: {n}", n in have, "python scripts/setup_mcp.py coding")
             for n in ("context7", "playwright", "postman", "firebase", "docker", "postgres", "github")]
    for name, ok, fix in rows:
        print(f"  [{'x' if ok else ' '}] {name}" + ("" if ok else f"   -> {fix}"))
    print("\n  Miro: python scripts/setup_mcp.py miro (sign-in can't be checked from here)")


if __name__ == "__main__":
    parts = {"status": status, "google": google, "coding": coding, "jobs": jobs, "miro": miro}
    if len(sys.argv) < 2 or sys.argv[1] not in parts:
        print(__doc__)
        sys.exit(1)
    parts[sys.argv[1]]()
