"""
The Stage: a live canvas of panels Jarvis opens while it works — web pages,
article summaries, files, images, notes and a 3-D globe.

All state lives here. Every change is pushed to connected dashboards over
Server-Sent Events (/api/stage/events), so voice commands and mouse edits (drag,
resize, close) work on the same layout, and is saved to disk (see load()) so the
Stage comes back as you left it the next time Jarvis starts. Closing panels or
clearing the Stage can be undone (restore_removed()).

Layout: a 12 x 12 grid that fills the screen. In "auto" mode the panels are tiled
by recursively splitting the screen, giving each panel area in proportion to its
weight (pages and maps need more room than a list of key points). "focus" gives
one panel most of the screen; "columns"/"rows" split it evenly. Dragging a panel
switches to "custom" until the next arrange command.
"""

import itertools
import json
import os
import queue
import threading
import time

COLS = ROWS = 12
MAX_PANELS = 8
_CELL_ASPECT = 1.6     # a grid cell is ~1.6x wider than tall on a 16:9 screen

# Relative screen area each kind wants; "size" from a voice command overrides it
KIND_WEIGHT = {"page": 4.0, "map": 3.5, "file": 3.0, "code": 2.6, "image": 2.5, "note": 1.6, "summary": 1.5}
SIZE_WEIGHT = {"small": 1.0, "medium": 2.0, "large": 4.0, "huge": 7.0}

_lock      = threading.RLock()
_ids       = itertools.count(1)
_panels: dict = {}              # id -> {"id","kind","title","data","weight","created"}
_order: list  = []              # panel ids, in tiling order
_private: dict = {}             # id -> server-only data (file paths, article text)
_layout: list = []              # react-grid-layout items: {"i","x","y","w","h"}
_view: dict   = {"mode": "auto", "focus": None}
_highlights: list = []          # newest last: {"id","panel","target","label","time"}
_active_hl  = None              # id of the highlight the beam points at
_rev        = 0
_route      = "/"               # where dashboards should be; sent to late joiners too

# Called when the Stage should be shown but no dashboard is connected (opens the desktop app)
open_dashboard = None

_undo = None                    # {"label", "items", "view", "layout"}: what the last close/clear removed
_store_path = None              # where the Stage is saved between runs (set by load())
_save_timer = None

_clients: dict = {}             # queue -> {"desktop": bool}
_extract_waits: dict = {}       # request id -> {"event", "result"}


# ── Subscribers (SSE) ─────────────────────────────────────────────────────────
def subscribe(desktop=False):
    q = queue.Queue()
    with _lock:
        _clients[q] = {"desktop": desktop}
    return q


def unsubscribe(q):
    with _lock:
        _clients.pop(q, None)


def has_clients(desktop_only=False):
    with _lock:
        return any(c["desktop"] or not desktop_only for c in _clients.values())


def _emit(event):
    with _lock:
        targets = list(_clients)
    for q in targets:
        q.put(event)


def snapshot():
    with _lock:
        return {
            "rev":        _rev,
            "panels":     [_panels[i] for i in _order],
            "layout":     list(_layout),
            "view":       dict(_view),
            "highlights": list(_highlights),
            "active":     _active_hl,
            "route":      _route,
            "undo":       _undo["label"] if _undo else None,
        }


def _changed():
    global _rev
    _rev += 1
    _emit({"type": "state", "state": snapshot()})
    _schedule_save()


def set_route(to):
    """The user switched tabs in the dashboard: remember it (for restarts) without moving anyone."""
    global _route
    if to in ("/", "/stage") and to != _route:
        _route = to
        _schedule_save()


def navigate(to):
    """Switch every dashboard to "/stage" or "/" (opening the desktop app if none is connected)."""
    global _route
    _route = to
    _schedule_save()
    if to == "/stage" and not has_clients() and open_dashboard:
        open_dashboard()      # it receives the route in its first snapshot
    _emit({"type": "navigate", "to": to})


# ── Layout ────────────────────────────────────────────────────────────────────
def _split(items, x, y, w, h, out):
    """Recursively split the rectangle, sizing each half by the weight it holds."""
    if not items:
        return
    if len(items) == 1:
        out.append({"i": items[0][0], "x": x, "y": y, "w": w, "h": h})
        return
    total = sum(wt for _, wt in items)
    best_k, best_diff, acc = 1, float("inf"), 0.0
    for k in range(1, len(items)):          # keep order: earlier panels go left/top
        acc += items[k - 1][1]
        if abs(acc - total / 2) < best_diff:
            best_k, best_diff = k, abs(acc - total / 2)
    first, second = items[:best_k], items[best_k:]
    frac = sum(wt for _, wt in first) / total
    if w * _CELL_ASPECT >= h * 1.2 and w >= 4:   # wide region: side by side (never < 2 cols)
        w1 = min(w - 2, max(2, round(w * frac)))
        _split(first, x, y, w1, h, out)
        _split(second, x + w1, y, w - w1, h, out)
    elif h >= 2:
        h1 = min(h - 1, max(1, round(h * frac)))
        _split(first, x, y, w, h1, out)
        _split(second, x, y + h1, w, h - h1, out)
    else:                                    # no room left to split: stack
        for i, (pid, _) in enumerate(items):
            out.append({"i": pid, "x": x, "y": y + i, "w": w, "h": 1})


def _even(ids, horizontal):
    out, n = [], len(ids)
    for k, pid in enumerate(ids):
        if horizontal:
            x0, x1 = round(k * COLS / n), round((k + 1) * COLS / n)
            out.append({"i": pid, "x": x0, "y": 0, "w": max(1, x1 - x0), "h": ROWS})
        else:
            y0, y1 = round(k * ROWS / n), round((k + 1) * ROWS / n)
            out.append({"i": pid, "x": 0, "y": y0, "w": COLS, "h": max(1, y1 - y0)})
    return out


def _relayout():
    global _layout
    mode, focus = _view["mode"], _view["focus"]
    if mode == "custom":
        known = {item["i"] for item in _layout}
        _layout = [item for item in _layout if item["i"] in _panels]
        if all(pid in known for pid in _order):
            return
        mode = _view["mode"] = "auto"        # a new panel arrived: re-tile around it
    out = []
    if mode == "focus" and focus in _panels and len(_order) > 1:
        others = [(pid, _panels[pid]["weight"]) for pid in _order if pid != focus]
        out.append({"i": focus, "x": 0, "y": 0, "w": 8, "h": ROWS})
        _split(others, 8, 0, COLS - 8, ROWS, out)
    elif mode in ("columns", "rows") and len(_order) <= 4:
        out = _even(_order, horizontal=(mode == "columns"))
    else:
        _split([(pid, _panels[pid]["weight"]) for pid in _order], 0, 0, COLS, ROWS, out)
    _layout = out


def arrange(mode, focus_ref=None):
    """mode: auto | focus | columns | rows."""
    with _lock:
        if mode == "focus":
            pid = resolve(focus_ref) or (_order[-1] if _order else None)
            if not pid:
                return False
            _view.update(mode="focus", focus=pid)
        else:
            _view.update(mode=mode if mode in ("auto", "columns", "rows") else "auto", focus=None)
        _relayout()
        _changed()
    return True


def set_client_layout(items):
    """A layout dragged/resized in the dashboard."""
    with _lock:
        global _layout
        _layout = [{k: int(it[k]) if k != "i" else str(it[k]) for k in ("i", "x", "y", "w", "h")}
                   for it in items if str(it.get("i")) in _panels]
        _view.update(mode="custom", focus=None)
        _changed()


def resize(ref, size):
    """size: small | medium | large | huge, or bigger / smaller relative to now."""
    with _lock:
        pid = resolve(ref)
        if not pid:
            return False
        if size == "bigger":
            _panels[pid]["weight"] *= 1.8
        elif size == "smaller":
            _panels[pid]["weight"] /= 1.8
        elif size in SIZE_WEIGHT:
            _panels[pid]["weight"] = SIZE_WEIGHT[size]
        else:
            return False
        if _view["mode"] == "custom":
            _view["mode"] = "auto"
        _relayout()
        _changed()
    return True


def move(ref, position):
    """position: first/left/top → start of the tiling order; last/right/bottom → end."""
    with _lock:
        pid = resolve(ref)
        if not pid:
            return False
        _order.remove(pid)
        if position in ("first", "left", "top", "start"):
            _order.insert(0, pid)
        else:
            _order.append(pid)
        if _view["mode"] == "custom":
            _view["mode"] = "auto"
        _relayout()
        _changed()
    return True


def swap(ref_a, ref_b):
    with _lock:
        a, b = resolve(ref_a), resolve(ref_b)
        if not a or not b or a == b:
            return False
        ia, ib = _order.index(a), _order.index(b)
        _order[ia], _order[ib] = b, a
        rects = {it["i"]: dict(it) for it in _layout}
        if _view["mode"] == "custom" and a in rects and b in rects:   # trade rectangles in place
            for it in _layout:
                if it["i"] in (a, b):
                    src = rects[b if it["i"] == a else a]
                    it.update(x=src["x"], y=src["y"], w=src["w"], h=src["h"])
        else:
            _relayout()
        _changed()
    return True


# ── Panels ────────────────────────────────────────────────────────────────────
def add(kind, title, data=None, private=None, size=None, focus=False, show=True):
    """Add a panel, re-tile, and switch the dashboard to the Stage. Returns its id."""
    with _lock:
        pid = f"p{next(_ids)}"
        _panels[pid] = {"id": pid, "kind": kind, "title": title, "data": data or {},
                        "weight": SIZE_WEIGHT.get(size, KIND_WEIGHT.get(kind, 2.0)),
                        "created": time.time()}
        _private[pid] = private or {}
        _order.append(pid)
        evicted = []
        while len(_order) > MAX_PANELS:      # drop the oldest panel that isn't in focus
            victim = next(p for p in _order if p != _view["focus"])
            evicted.append(_remove(victim))
        if evicted:
            _remember("Auto-closed " + ", ".join(e["panel"]["title"] for e in evicted)[:80], evicted)
        if focus:
            _view.update(mode="focus", focus=pid)
        _relayout()
        _changed()
    if show:
        navigate("/stage")
    return pid


def update(pid, title=None, **data):
    with _lock:
        if pid not in _panels:
            return False
        if title:
            _panels[pid]["title"] = title
        _panels[pid]["data"] = {**_panels[pid]["data"], **data}
        _changed()
    return True


def private(pid):
    with _lock:
        return _private.setdefault(pid, {})


def get(pid):
    with _lock:
        p = _panels.get(pid)
        return dict(p) if p else None


def _remove(pid):
    """Take a panel off the Stage. Returns what's needed to put it back."""
    record = {"panel": _panels[pid], "private": _private.get(pid, {}), "index": _order.index(pid)}
    del _panels[pid]
    _private.pop(pid, None)
    _order.remove(pid)
    _highlights[:] = [h for h in _highlights if h["panel"] != pid]
    if _view["focus"] == pid:
        _view.update(mode="auto", focus=None)
    return record


def _remember(label, items, view=None, layout=None):
    """Keep what the last close/clear removed, so it can be undone."""
    global _undo
    _undo = {"label": label, "items": items, "view": view or dict(_view),
             "layout": layout if layout is not None else [dict(it) for it in _layout]}


def close(ref):
    with _lock:
        pid = resolve(ref)
        if not pid:
            return False
        view, layout = dict(_view), [dict(it) for it in _layout]
        record = _remove(pid)
        _remember(f"Close {record['panel']['title']}"[:80], [record], view, layout)
        _relayout()
        _changed()
    return True


def clear():
    """Close every panel (undoable with restore_removed)."""
    with _lock:
        if _order:
            view, layout = dict(_view), [dict(it) for it in _layout]
            items = []
            for index, pid in enumerate(list(_order)):
                record = _remove(pid)
                record["index"] = index      # its position before anything was removed
                items.append(record)
            _remember("Clear stage", items, view, layout)
        _view.update(mode="auto", focus=None)
        _relayout()
        _changed()


def restore_removed():
    """Bring back the panels the last close/clear removed. Returns how many came back."""
    global _undo, _layout
    with _lock:
        if not _undo:
            return 0
        was_empty = not _order
        back = 0
        for item in sorted(_undo["items"], key=lambda it: it["index"]):
            pid = item["panel"]["id"]
            if pid in _panels or not _still_valid(item["panel"], item["private"]):
                continue
            _panels[pid] = item["panel"]
            _private[pid] = item["private"]
            _order.insert(min(item["index"], len(_order)), pid)
            back += 1
        if was_empty or (len(_undo["items"]) == 1 and _undo["view"].get("mode") == "custom"):
            # Put things back exactly where they were
            _view.update(_undo["view"])
            if _view["mode"] == "custom":
                _layout = [it for it in _undo["layout"] if it["i"] in _panels]
        if _view.get("focus") not in _panels:
            _view.update(mode="auto" if _view["mode"] == "focus" else _view["mode"], focus=None)
        _undo = None
        _relayout()
        _changed()
    if back:
        navigate("/stage")
    return back


# ── Saving between runs ───────────────────────────────────────────────────────
def _still_valid(panel, priv):
    """Image and file panels need their file to still exist."""
    if panel["kind"] in ("image", "file"):
        return bool(priv.get("path")) and os.path.isfile(priv["path"])
    return True


def _save_now():
    if not _store_path:
        return
    with _lock:
        data = json.dumps({
            "version": 1,
            "panels": [_panels[i] for i in _order],
            "private": {i: _private.get(i, {}) for i in _order},
            "layout": _layout, "view": _view, "highlights": _highlights,
            "route": _route, "undo": _undo,
        })
    try:
        tmp = _store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, _store_path)      # never leave a half-written file behind
    except OSError as e:
        print(f"  Could not save the Stage: {e}")


def _schedule_save():
    """Save shortly after the last change (a burst of updates becomes one write)."""
    global _save_timer
    if not _store_path:
        return
    if _save_timer:
        _save_timer.cancel()
    _save_timer = threading.Timer(0.5, _save_now)
    _save_timer.daemon = True
    _save_timer.start()


def flush():
    """Write any pending save immediately (on shutdown)."""
    if _save_timer:
        _save_timer.cancel()
    _save_now()


def load(path):
    """Restore the Stage saved by the last run, and keep saving to `path`. Returns the panel count."""
    global _store_path, _layout, _route, _undo, _ids
    _store_path = path
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"  Could not read the saved Stage ({e}) — starting with an empty one.")
        return 0
    with _lock:
        for panel in data.get("panels", []):
            pid = panel["id"]
            priv = data.get("private", {}).get(pid, {})
            if not _still_valid(panel, priv):
                continue
            if panel["kind"] == "file":       # the file may have been edited outside Jarvis
                try:
                    with open(priv["path"], encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                panel["data"] = {**panel["data"], "content": content, "proposal": None,
                                 "version": panel["data"].get("version", 0) + 1}
            _panels[pid] = panel
            _private[pid] = priv
            _order.append(pid)
        _layout = [it for it in data.get("layout", []) if it.get("i") in _panels]
        _view.update(data.get("view") or {})
        if _view.get("focus") not in _panels:
            _view.update(mode="auto" if _view.get("mode") == "focus" else _view.get("mode", "auto"), focus=None)
        _highlights[:] = [h for h in data.get("highlights", []) if h.get("panel") in _panels]
        _route = data.get("route", "/") if _order else "/"
        _undo = data.get("undo")
        # keep new ids from colliding with restored ones (and with undoable panels)
        undo_ids = [it["panel"]["id"] for it in (_undo or {}).get("items", [])]
        used = [int(x[1:]) for x in list(_panels) + [h["id"] for h in _highlights] + undo_ids if x[1:].isdigit()]
        _ids = itertools.count(max(used, default=0) + 1)
        _relayout()
    return len(_order)


def panels():
    with _lock:
        return [dict(_panels[i]) for i in _order]


_KIND_WORDS = {
    "page": ("page", "article", "website", "site", "web", "browser"),
    "summary": ("summary", "key points", "points", "notes on"),
    "file": ("file", "document", "doc", "code", "editor"),
    "image": ("image", "picture", "photo", "screenshot"),
    "map": ("map", "globe", "earth"),
    "note": ("note", "list", "card"),
    "code": ("claude code", "coding", "build log", "progress", "code panel"),
}
_ORDINALS = {"first": 1, "one": 1, "second": 2, "two": 2, "third": 3, "three": 3, "fourth": 4,
             "four": 4, "fifth": 5, "five": 5, "sixth": 6, "six": 6, "seventh": 7, "eighth": 8}


def resolve(ref):
    """
    Turn a voice reference into a panel id: a panel id, a number ("2", "panel two",
    "the second one"), a kind ("the map", "the article"), or words from the title.
    Empty → the focused panel, else the most recent one.
    """
    with _lock:
        if not _order:
            return None
        ref = str(ref or "").strip().lower()
        if not ref:
            return _view["focus"] if _view["focus"] in _panels else _order[-1]
        if ref in _panels:
            return ref
        words = ref.replace("#", " ").split()
        for w in words:
            n = int(w) if w.isdigit() else _ORDINALS.get(w)
            if n and 1 <= n <= len(_order):
                return _order[n - 1]
        if ref in ("last", "latest", "newest", "that", "this", "it"):
            return _order[-1]
        for kind, names in _KIND_WORDS.items():
            if any(name in ref for name in names):
                matches = [p for p in _order if _panels[p]["kind"] == kind]
                if matches:
                    return matches[-1]
        best, best_score = None, 0
        for pid in _order:
            title = _panels[pid]["title"].lower()
            score = sum(1 for w in words if len(w) > 2 and w in title)
            if score > best_score:
                best, best_score = pid, score
        return best


def describe():
    """One line per panel for Claude's system prompt, so "the map" / "panel 2" make sense."""
    with _lock:
        if not _order:
            return ""
        lines = []
        for n, pid in enumerate(_order, 1):
            p = _panels[pid]
            extra = p["data"].get("url") or p["data"].get("name") or p["data"].get("project") or ""
            lines.append(f'{n}. {p["kind"]} "{p["title"]}"' + (f" ({extra})" if extra else ""))
        focus = _order.index(_view["focus"]) + 1 if _view["focus"] in _panels else None
        return ("\n".join(lines) + f"\nLayout: {_view['mode']}" + (f", panel {focus} in focus" if focus else ""))


# ── Highlights ────────────────────────────────────────────────────────────────
def highlight(pid, target, label=""):
    """
    Point something out. target depends on the panel kind:
      page/note/summary → {"quote": exact text}     image → {"region": [x, y, w, h] in 0-1}
      file → {"lines": [first, last]}               map   → {"place": index}
    """
    global _active_hl
    with _lock:
        if pid not in _panels:
            return None
        hid = f"h{next(_ids)}"
        _highlights[:] = [h for h in _highlights if h["panel"] != pid][-6:]   # one live mark per panel
        _highlights.append({"id": hid, "panel": pid, "target": target, "label": label, "time": time.time()})
        _active_hl = hid
        _changed()
    return hid


def clear_highlights():
    global _active_hl
    with _lock:
        _highlights.clear()
        _active_hl = None
        _changed()


# ── Text from the desktop app's live page (for JS-heavy sites) ───────────────
def request_extract(pid, timeout=12.0):
    """Ask a desktop dashboard to read the rendered page in panel `pid`. Returns dict or None."""
    if not has_clients(desktop_only=True):
        return None
    rid = f"x{next(_ids)}"
    wait = {"event": threading.Event(), "result": None}
    with _lock:
        _extract_waits[rid] = wait
    _emit({"type": "extract", "panel": pid, "request": rid})
    wait["event"].wait(timeout)
    with _lock:
        _extract_waits.pop(rid, None)
    return wait["result"]


def deliver_extract(rid, result):
    with _lock:
        wait = _extract_waits.get(rid)
    if wait:
        wait["result"] = result
        wait["event"].set()


# ── Content helpers ───────────────────────────────────────────────────────────
_LANGS = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
          ".tsx": "typescript", ".html": "html", ".css": "css", ".json": "json", ".md": "markdown",
          ".sh": "shell", ".bat": "bat", ".ps1": "powershell", ".sql": "sql", ".yml": "yaml",
          ".yaml": "yaml", ".xml": "xml", ".java": "java", ".c": "c", ".cpp": "cpp", ".cs": "csharp",
          ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".toml": "ini", ".ini": "ini"}


def _find_open(kind, key, value):
    with _lock:
        for pid in _order:
            if _panels[pid]["kind"] == kind and _private.get(pid, {}).get(key) == value:
                return pid
    return None


def add_image(path, caption="", title=None):
    """Show an image file. It is served by /api/stage/media/<panel id> — never by path."""
    pid = _find_open("image", "path", path)
    if pid:
        update(pid, caption=caption)
        navigate("/stage")
        return pid
    name = os.path.basename(path)
    pid = add("image", title or name, {"name": name, "caption": caption}, private={"path": path})
    update(pid, src=f"/api/stage/media/{pid}?v={int(os.path.getmtime(path))}")
    return pid


def open_file(path):
    """Show a text file in the editor panel (reusing its panel if it is already open)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    pid = _find_open("file", "path", path)
    if pid:
        with _lock:
            version = _panels[pid]["data"].get("version", 0) + 1
        update(pid, content=content, version=version, proposal=None)
        navigate("/stage")
        return pid
    name = os.path.basename(path)
    return add("file", name, {"name": name, "content": content, "version": 1, "proposal": None,
                              "language": _LANGS.get(os.path.splitext(name)[1].lower(), "plaintext")},
               private={"path": path})


def save_file(pid, content):
    """Save edits typed in the dashboard. Doesn't bump "version": the editor already has them."""
    path = private(pid).get("path")
    if not path:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    with _lock:
        _panels[pid]["data"] = {**_panels[pid]["data"], "content": content}
        global _rev
        _rev += 1
    _emit({"type": "file_saved", "panel": pid, "rev": _rev})
    _schedule_save()
    return True


def propose_file(pid, content, note=""):
    """Jarvis's rewrite, shown as a diff to accept or reject."""
    return update(pid, proposal=content, proposal_note=note)


def resolve_proposal(pid, accept):
    with _lock:
        p = _panels.get(pid)
        if not p or p["data"].get("proposal") is None:
            return False
        proposal, version = p["data"]["proposal"], p["data"].get("version", 0)
    if accept:
        with open(private(pid)["path"], "w", encoding="utf-8") as f:
            f.write(proposal)
        return update(pid, content=proposal, version=version + 1, proposal=None, proposal_note="")
    return update(pid, proposal=None, proposal_note="")


def show_places(places, route=False, title=None):
    """Put places ({"name","lat","lon"}) on the globe and fly to the last one."""
    with _lock:
        pid = next((p for p in _order if _panels[p]["kind"] == "map"), None)
    if pid:
        with _lock:
            flight = _panels[pid]["data"].get("flight", 0) + 1
        update(pid, title=title, places=places, route=route, focus=len(places) - 1, flight=flight)
        navigate("/stage")
    else:
        pid = add("map", title or "Map", {"places": places, "route": route,
                                          "focus": len(places) - 1, "flight": 1})
    return pid
