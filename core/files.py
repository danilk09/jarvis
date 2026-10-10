"""
Local lookups: file index, browser bookmarks, installed apps and workspaces.
All indexes are built in the background so startup stays fast.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser

from . import config

FILE_INDEX: list = []
BOOKMARKS:  list = []
APPS:       list = []   # [{"name": ..., "id": AppUserModelID}] from the Start menu


# ── File index ────────────────────────────────────────────────────────────────
def build_file_index():
    global FILE_INDEX
    print("  Building file index (scanning your directories)...")
    roots = [os.path.expanduser(p) for p in (
        "~/Desktop", "~/Documents", "~/Downloads", "~/Pictures",
        "~/Music", "~/Videos", "~/OneDrive", "~/Google Drive",
    )]
    skip_dirs = {"node_modules", "__pycache__", "venv", ".venv", "site-packages", "build", "dist"}
    paths   = []
    visited = set()   # real paths already walked (Desktop/Documents are often inside OneDrive)
    for root in roots:
        if not os.path.exists(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            real = os.path.realpath(dirpath).lower()
            if real in visited:
                dirnames[:] = []
                continue
            visited.add(real)
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skip_dirs]
            for f in filenames:
                paths.append(os.path.join(dirpath, f))
    FILE_INDEX = paths
    print(f"  File index built: {len(FILE_INDEX)} files found.")


def fuzzy_match_files(keyword, threshold=60, limit=20):
    keyword  = keyword.lower()
    kw_words = set(keyword.split())
    scored   = []
    for p in FILE_INDEX:
        name = os.path.basename(p).lower()
        name_no_ext = os.path.splitext(name)[0]
        if keyword in name:
            scored.append((100, p))
            continue
        name_words = set(name_no_ext.replace("_", " ").replace("-", " ").split())
        overlap = len(kw_words & name_words)
        if overlap == 0:
            continue
        shorter, longer = sorted([keyword, name_no_ext], key=len)
        char_score = sum(1 for c in shorter if c in longer) / max(len(longer), 1) * 100
        score = (overlap / max(len(kw_words), 1) * 60) + (char_score * 0.4)
        if score >= threshold:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]


# ── Bookmarks ─────────────────────────────────────────────────────────────────
_BOOKMARK_FILES = {
    "Chrome": "~/AppData/Local/Google/Chrome/User Data/Default/Bookmarks",
    "Edge":   "~/AppData/Local/Microsoft/Edge/User Data/Default/Bookmarks",
}


def _read_bookmarks(browser, path):
    path = os.path.expanduser(path)
    found = []
    if not os.path.exists(path):
        return found
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        def walk(node):
            if node.get("type") == "url":
                found.append({"name": node.get("name", ""), "url": node.get("url", "")})
            for child in node.get("children", []):
                walk(child)

        for root in data.get("roots", {}).values():
            walk(root)
    except Exception as e:
        print(f"  Could not read {browser} bookmarks: {e}")
    return found


def build_bookmark_index():
    global BOOKMARKS
    BOOKMARKS = [b for browser, path in _BOOKMARK_FILES.items() for b in _read_bookmarks(browser, path)]
    print(f"  Bookmark index built: {len(BOOKMARKS)} bookmarks found.")


def find_bookmark(keyword):
    keyword = keyword.lower()
    exact = [b for b in BOOKMARKS if b["name"].lower() == keyword]
    return (exact or [b for b in BOOKMARKS if keyword in b["name"].lower()] or [None])[0]


# ── Installed apps ────────────────────────────────────────────────────────────
def build_app_index():
    """One PowerShell call at startup instead of one per "open X" command."""
    global APPS
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        data = json.loads(out) if out else []
        if isinstance(data, dict):
            data = [data]
        APPS = [{"name": a["Name"], "id": a["AppID"]} for a in data if a.get("Name") and a.get("AppID")]
        print(f"  App index built: {len(APPS)} apps found.")
    except Exception as e:
        print(f"  Could not build app index: {e}")


def find_app(name):
    """Best Start-menu match: exact name, then prefix, then substring (shortest wins)."""
    name = name.lower().strip()
    if not name:
        return None
    for test in (lambda n: n == name, lambda n: n.startswith(name), lambda n: name in n):
        hits = [a for a in APPS if test(a["name"].lower())]
        if hits:
            return min(hits, key=lambda a: len(a["name"]))
    return None


def closest_name(heard, names, cutoff=0.8, margin=0.08):
    """The name `heard` most likely was, ignoring spaces ("this cord" → "Discord"), or None
    unless it's a close match and clearly closer than the runner-up."""
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    target = squash(heard)
    if len(target) < 4:
        return None
    scored = sorted(((difflib.SequenceMatcher(None, target, squash(n)).ratio(), n) for n in names if n),
                    reverse=True)
    if not scored or scored[0][0] < cutoff:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < margin and squash(scored[1][1]) != squash(scored[0][1]):
        return None
    return scored[0][1]


def open_app(name) -> bool:
    """Launch an app by (fuzzy) name. Returns False if nothing matching was found."""
    if not APPS:
        build_app_index()
    app = find_app(name)
    if app:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['id']}"])
        return True
    exe = shutil.which(name)   # e.g. "notepad", "calc"
    if exe:
        subprocess.Popen([exe])
        return True
    return False


# ── Workspaces ────────────────────────────────────────────────────────────────
def load_workspaces():
    if os.path.exists(config.WORKSPACES_FILE):
        with open(config.WORKSPACES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def find_workspace(name, workspaces):
    if name in workspaces:
        return name
    for key in workspaces:
        if name.lower() in key.lower() or key.lower() in name.lower():
            return key
    return None


def open_workspace(name, workspaces):
    key = find_workspace(name, workspaces)
    if not key:
        return False, f"No workspace named '{name}' found."
    opened = []
    for item in workspaces[key].get("items", []):
        kind = item.get("type")
        path = item.get("path", "")
        if kind == "url":
            webbrowser.open(path)
            opened.append(f"URL: {path}")
        elif kind == "vscode":
            subprocess.Popen(["code", path], shell=True)
            opened.append(f"VSCode: {path}")
        elif kind == "file":
            os.startfile(path)
            opened.append(f"File: {path}")
        elif kind == "app":
            subprocess.Popen(f'start "" "{path}"', shell=True)
            opened.append(f"App: {path}")
        time.sleep(0.3)
    return True, f"Opened workspace '{key}': {', '.join(opened)}"
