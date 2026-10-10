"""
Connect Claude Code to GitHub (GitHub's official MCP server), so the coding runs Jarvis
starts can read issues, open pull requests and check CI.

    1. Create a token: github.com -> Settings -> Developer settings -> Personal access tokens
       -> Fine-grained tokens -> Generate. Repository access: all your repos. Permissions:
       Contents, Issues, Pull requests, Actions (read), Metadata (read) — read and write
       where offered.
    2. Put it in .env:   GITHUB_TOKEN=github_pat_...
    3. Run:              python scripts/setup_github_mcp.py

It registers the server for your user (every project), replacing an earlier "github"
entry. The token is passed straight to `claude mcp add` and never printed.
"""

import os
import shutil
import subprocess
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
URL = "https://api.githubcopilot.com/mcp/"


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        sys.exit("No GITHUB_TOKEN in .env — see the steps at the top of this file.")
    claude = shutil.which("claude")
    if not claude:
        sys.exit("Claude Code isn't on PATH (npm install -g @anthropic-ai/claude-code).")
    subprocess.run([claude, "mcp", "remove", "github", "-s", "user"], capture_output=True)
    added = subprocess.run([claude, "mcp", "add", "--transport", "http", "--scope", "user", "github", URL,
                            "--header", f"Authorization: Bearer {token}"], capture_output=True, text=True)
    if added.returncode != 0:
        sys.exit("claude mcp add failed:\n" + (added.stderr or added.stdout).replace(token, "***"))
    check = subprocess.run([claude, "mcp", "get", "github"], capture_output=True, text=True, timeout=60)
    print((check.stdout or check.stderr).replace(token, "***"))


if __name__ == "__main__":
    main()
