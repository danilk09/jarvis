"""
Coding projects across the Pi and the PC (Mobile Jarvis Phase 3, M6).

Claude Code runs on the Pi (the brain), but the user reviews and keeps working at the PC:

- After a coding run in a GitHub repo, Jarvis makes sure its branch ("jarvis", or a
  jarvis-<task> one you asked for) is pushed (Claude Code is told to push it, this is the
  backstop). Never main: that's Claude Code's deny rules for its own pushes, and Jarvis only
  ever pushes Jarvis branches.
- A new project (jarvis_coding/<name>, no GitHub remote) gets one question after its first
  good run: "Want me to put it on GitHub as a private repo?" → code command "github" →
  publish(): creates the repo with GITHUB_TOKEN (fine-grained token: Administration and
  Contents read/write), commits, pushes main (its first push).
- Every new repo and pushed branch goes into jarvis_memory/coding_ledger.json. "Update my
  coding sessions" (repo_sync) sends what changed since the last sync to the PC
  (repo_sync_pc, through core/pc.py), which clones new repos into REPO_DIRS[0], fetches and
  fast-forwards the others (never touching local changes) and lists the branches to review.

On the Pi, git talks to GitHub with GITHUB_TOKEN (auth_env(): a credential helper for
github.com only, also given to Claude Code), so pushes to private repos work without a login.
"""

import json
import os
import re
import subprocess
import threading
import time

import requests

from . import config, stage
from .registry import action
from .tts import speak

_LEDGER = os.path.join(config.MEMORY_DIR, "coding_ledger.json")
_lock = threading.Lock()
_HELPER = '!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'


# ── git with the token ────────────────────────────────────────────────────────
def auth_env(env=None):
    """Environment that lets git (and Claude Code's git) push to github.com with GITHUB_TOKEN.
    Only on the Pi: on the PC, your own Git login (Credential Manager) already does this."""
    env = dict(os.environ if env is None else env)
    if config.HEADLESS and config.GITHUB_TOKEN:
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        env.update({"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "credential.https://github.com.helper",
                    f"GIT_CONFIG_VALUE_{n}": _HELPER, "GITHUB_TOKEN": config.GITHUB_TOKEN})
    return env


def git(path, *args, timeout=60):
    """(exit code, output) of git in `path`, with the token (on the Pi)."""
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=auth_env(),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def github_repo(path):
    """"owner/name" of the repo's GitHub origin, or ""."""
    code, url = git(path, "remote", "get-url", "origin", timeout=10)
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", url) if code == 0 else None
    return f"{m.group(1)}/{m.group(2)}" if m else ""


# ── The ledger ────────────────────────────────────────────────────────────────
def _load():
    try:
        with open(_LEDGER, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    os.makedirs(config.MEMORY_DIR, exist_ok=True)
    tmp = _LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, _LEDGER)


def record(kind, repo, name, branch="", path=""):
    """kind: "new_repo" or "branch". The same branch again only moves its time."""
    with _lock:
        data = _load()
        events = [e for e in data.get("events", [])
                  if not (kind == "branch" and e.get("kind") == "branch" and e.get("repo") == repo
                          and e.get("branch") == branch)]
        events.append({"time": time.time(), "kind": kind, "repo": repo, "name": name, "branch": branch,
                       "path": path})
        data["events"] = events[-300:]
        _write(data)


def pending():
    """What changed since the last sync, per repo: [{"repo", "name", "path", "branches": [...]}]."""
    data = _load()
    since = data.get("synced", 0)
    repos = {}
    for e in data.get("events", []):
        if e.get("time", 0) <= since:
            continue
        r = repos.setdefault(e["repo"], {"repo": e["repo"], "name": e.get("name") or e["repo"].split("/")[-1],
                                         "path": e.get("path", ""), "branches": []})
        if e.get("kind") == "branch" and e.get("branch") and e["branch"] not in r["branches"]:
            r["branches"].append(e["branch"])
    return list(repos.values())


def _synced(at):
    with _lock:
        data = _load()
        data["synced"] = at
        _write(data)


# ── After a coding run ────────────────────────────────────────────────────────
def after_run(name, path, status, ask_github, work_branch=""):
    """coding._finish, once a run is over: push its Jarvis branch (in the background), or
    offer GitHub for a new project. Returns True if it asked something."""
    if status != "done" or not path or not os.path.isdir(path):
        return False
    repo = github_repo(path) if os.path.isdir(os.path.join(path, ".git")) else ""
    if repo:
        threading.Thread(target=_push_branch, args=(name, path, repo, work_branch), daemon=True, name="push").start()
        return False
    if ask_github and config.GITHUB_TOKEN:
        speak(f"By the way, {name.replace('_', ' ')} only lives on {'the Pi' if config.HEADLESS else 'this PC'} "
              "for now. Want me to put it on GitHub as a private repo?")
        return True
    return False


def _push_branch(name, path, repo, work_branch=""):
    """Push the run's branch: a Jarvis one, or the one you chose to work on (never main/master)."""
    from .coding import is_jarvis_branch
    code, branch = git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    chosen = branch == work_branch and branch not in ("main", "master")
    if code or not (is_jarvis_branch(branch) or chosen):
        return
    code, out = git(path, "push", "-u", "origin", branch, timeout=120)
    if code:
        print(f"  [repos] couldn't push {branch} in {name}: {out[-300:]}")
        return
    print(f"  [repos] {name}: {branch} is on GitHub")
    record("branch", repo, name, branch, path)


# ── New project → private GitHub repo ─────────────────────────────────────────
def publish(name, path):
    """Create a private GitHub repo for the project at `path` and push it. Returns a status."""
    if not config.GITHUB_TOKEN:
        speak("I need a GitHub token for that. Add GITHUB_TOKEN to the .env file.")
        return "GitHub: no token"
    if github_repo(path):
        speak(f"{name.replace('_', ' ')} is already on GitHub.")
        return "GitHub: already there"
    repo_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name.replace("_", "-")).strip("-") or "project"
    try:
        r = requests.post("https://api.github.com/user/repos", timeout=20,
                          headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}",
                                   "Accept": "application/vnd.github+json"},
                          json={"name": repo_name, "private": True,
                                "description": "Built with Claude Code through Jarvis"})
    except requests.RequestException as e:
        speak("I couldn't reach GitHub.")
        return f"GitHub: {e}"
    if r.status_code == 422:
        speak(f"There's already a repo called {repo_name} on your GitHub, so I left it alone.")
        return "GitHub: name taken"
    if r.status_code in (401, 403):
        speak("GitHub said no: the token needs permission to create repositories. "
              "Give it Administration and Contents read and write.")
        return f"GitHub: {r.status_code}"
    if r.status_code != 201:
        speak("GitHub couldn't create the repo.")
        return f"GitHub: {r.status_code} {r.text[:200]}"
    full = r.json()["full_name"]

    who = ["-c", f"user.name={config.GITHUB_USER}", "-c", f"user.email={config.GITHUB_USER}@users.noreply.github.com"]
    if not os.path.isdir(os.path.join(path, ".git")):
        git(path, "init", "-b", "main")
    git(path, "add", "-A")
    git(path, *who, "commit", "-m", "Initial commit (built with Claude Code through Jarvis)")
    git(path, "remote", "add", "origin", f"https://github.com/{full}.git")
    code, branch = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    code, out = git(path, "push", "-u", "origin", f"{branch}:main" if branch != "main" else "main", timeout=180)
    if code:
        speak("I created the repo, but pushing to it failed. The details are in the log.")
        print(f"  [repos] push failed: {out[-400:]}")
        return "GitHub: push failed"
    record("new_repo", full, name, path=path)
    speak(f"Done. {name.replace('_', ' ')} is on GitHub as a private repo.")
    return f"GitHub: created {full}"


# ── Catch-up at the PC ────────────────────────────────────────────────────────
@action("repo_sync",
        schema='{"type":"repo_sync"}',
        rules=['"update my coding sessions", "sync my repos/projects", "pull my projects", "catch me up on coding", '
               '"get the new branches" → type "repo_sync" (clones/pulls on the PC what Claude Code did on the Pi)'])
def _repo_sync(a, chain):
    todo = pending()
    if not todo:
        speak("Nothing new since your last catch-up.")
        return "Repo sync: nothing new"
    started = time.time()
    job = {"type": "repo_sync_pc", "repos": todo}
    if config.HEADLESS:
        from . import pc
        result = pc.run(job, chain)
    else:
        result = _repo_sync_pc(job, chain)
    if chain.get("sync_note"):
        stage.add("note", "Coding catch-up", {"markdown": chain["sync_note"]})
    if chain.get("synced"):
        _synced(started)
    return result


@action("repo_sync_pc", pc_only=True)
def _repo_sync_pc(a, chain):
    """On the PC: clone what's new, fast-forward what's there, list the branches to review."""
    base = config.REPO_DIRS[0]
    os.makedirs(base, exist_ok=True)
    cloned, pulled, fetched, failed, review = [], [], [], [], []
    for r in a.get("repos", []):
        name, repo = r["name"], r["repo"]
        if r.get("path") and os.path.isdir(r["path"]):
            continue                                      # this machine is where it was made
        path = _local_clone(repo) or os.path.join(base, repo.split("/")[-1])
        if not os.path.isdir(path):
            code, out = git(base, "clone", f"https://github.com/{repo}.git", path, timeout=300)
            (cloned if code == 0 else failed).append(name)
            if code:
                print(f"  [repos] clone {repo} failed: {out[-300:]}")
                continue
        else:
            git(path, "fetch", "--prune", "origin", timeout=120)
            _, dirty = git(path, "status", "--porcelain")
            code, _ = (1, "") if dirty else git(path, "pull", "--ff-only", timeout=120)
            (pulled if code == 0 else fetched).append(name)
        for b in r.get("branches", []):
            if git(path, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{b}")[0] == 0:
                review.append((name, b))
    parts = []
    if cloned:
        parts.append("Cloned " + ", ".join(n.replace("_", " ") for n in cloned) + ".")
    if pulled:
        parts.append("Updated " + ", ".join(n.replace("_", " ") for n in pulled) + ".")
    if fetched:
        parts.append(", ".join(n.replace("_", " ") for n in fetched)
                     + (" has" if len(fetched) == 1 else " have") + " local changes, so I only fetched.")
    if failed:
        parts.append("Couldn't clone " + ", ".join(failed) + ".")
    if review:
        parts.append(f"{len(review)} branch{'es' if len(review) > 1 else ''} waiting for your review"
                     + (": " + review[0][1] + " in " + review[0][0].replace("_", " ") if len(review) == 1 else
                        ". They're on the stage."))
    speak(" ".join(parts) or "Everything was already up to date.")
    md = ["## Coding catch-up", ""]
    md += [f"- Cloned **{n}**" for n in cloned] + [f"- Updated **{n}**" for n in pulled]
    md += [f"- Fetched **{n}** (local changes, not pulled)" for n in fetched] + [f"- Clone failed: {n}" for n in failed]
    if review:
        md += ["", "### Branches to review", ""] + [f"- `{b}` in **{n}**" for n, b in review]
    chain["sync_note"] = "\n".join(md)
    chain["synced"] = not failed
    return f"Repo sync: {len(cloned)} cloned, {len(pulled)} updated, {len(review)} to review"


def _local_clone(repo):
    """A folder in REPO_DIRS whose origin is this GitHub repo (it may be named differently)."""
    for d in config.REPO_DIRS:
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if os.path.isdir(os.path.join(p, ".git")) and github_repo(p).lower() == repo.lower():
                return p
    return ""
