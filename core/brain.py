"""
Command dispatch: sends the transcript plus recent history to Claude and parses
the single JSON object it returns ({"mode": "action"|"chat"|"none", ...}).
"""

import json
import os
import threading
import time

from . import agenda, config, memory, registry, stage, verbosity

CHAT_HISTORY: list = []   # user/assistant turns only; the system prompt is kept separately
_system_prompt = ""
_lock = threading.Lock()
_MAX_TURNS = 12           # messages kept in history (~6 exchanges) to bound cost
_HISTORY_FILE = os.path.join(config.MEMORY_DIR, "chat_history.json")

_ai_error_count = 0
_AI_ERROR_RESET_THRESHOLD = 3


def build_system_prompt(workspaces):
    actions = "\n".join(f'{{"mode":"action","actions":[{s}]}}' for s in registry.schemas())
    rules   = "\n".join(f"- {r}" for r in registry.rules())
    return f"""You are JARVIS. Return ONLY a single JSON object, no explanation.

WORKSPACES (use type "workspace"): {json.dumps(list(workspaces.keys()))}
BOOKMARKS searchable by keyword (use type "bookmark").
FILES searchable by keyword (use type "find_files").

ACTION TYPES (pick one; several actions may be chained in one "actions" list):
{actions}
{{"mode":"chat","reply":"<your answer>"}}
{{"mode":"none"}}

RULES:
- Multi-part requests → several actions in one list, e.g. "open Discord and play lofi" → {{"mode":"action","actions":[{{"type":"app","target":"Discord"}},{{"type":"music","command":"play","query":"lofi"}}]}}
{rules}
- Words like "open","find","search","launch","show" → ALWAYS mode "action"
- Any question or request for information that doesn't need real-time data → mode "chat" with a spoken reply
- The command is speech-to-text and may be misheard: read it by sound when a word makes no sense ("open this cord" → Discord, "jervis" → Jarvis)
- Unclear/filler → mode "none"
- Chat replies: no markdown, no lists, plain spoken sentences only. Answer only what was asked.
- End with a question only when you need the user's answer to continue (Jarvis then listens for it without the wake word) — never "anything else?".
- A command starting (Answering your question "...") is the user's reply to what you asked: act on it in that context.
- MEMORY (below, when present) is what you know about the user: use it to understand and personalize, e.g. "text my sister" when MEMORY names her.
"""


def init_chat_history(workspaces):
    global _system_prompt
    with _lock:
        _system_prompt = build_system_prompt(workspaces)
        CHAT_HISTORY.clear()


def restore_history():
    """Pick up the conversation from the last run if it ended recently (a quick restart)."""
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return 0
    if time.time() - saved.get("saved", 0) > config.CHAT_HISTORY_KEEP_MIN * 60:
        return 0
    with _lock:
        CHAT_HISTORY[:] = [m for m in saved.get("history", [])
                           if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        _trim()
        return len(CHAT_HISTORY)


def _save_history():
    try:
        os.makedirs(config.MEMORY_DIR, exist_ok=True)
        tmp = _HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved": time.time(), "history": CHAT_HISTORY}, f)
        os.replace(tmp, _HISTORY_FILE)
    except OSError as e:
        print(f"  Could not save chat history: {e}")


def remember(user_text, assistant_text=None):
    """Record a side-channel exchange (image analysis, web search, ...) for follow-up questions."""
    with _lock:
        CHAT_HISTORY.append({"role": "user", "content": user_text})
        if assistant_text is not None:
            CHAT_HISTORY.append({"role": "assistant", "content": assistant_text})
        _trim()
        _save_history()


def _trim():
    """Keep the last _MAX_TURNS messages, always starting on a user turn (the API requires it)."""
    if len(CHAT_HISTORY) > _MAX_TURNS:
        del CHAT_HISTORY[:len(CHAT_HISTORY) - _MAX_TURNS]
    while CHAT_HISTORY and CHAT_HISTORY[0]["role"] != "user":
        del CHAT_HISTORY[0]


def _parse(text):
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.split("```")[0]
    text = text.strip().replace("\\ ", " ").replace("\\.", ".")
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fall back to the first balanced {...} object in the text
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, c in enumerate(text[start:], start):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


parse_json = _parse

# Extra context for the system prompt, computed per call (e.g. the current coding project)
context_providers: list = []


def _stage_context():
    panels = stage.describe()
    if not panels:
        return ""
    return ("\n\nSTAGE — panels on screen now (refer to them by number, kind or title in "
            "\"panel\" fields):\n" + panels)


def log_usage(tag, usage):
    """One line per call: input tokens and how much of it came from the prompt cache."""
    read  = getattr(usage, "cache_read_input_tokens", 0) or 0
    wrote = getattr(usage, "cache_creation_input_tokens", 0) or 0
    total = usage.input_tokens + read + wrote
    if read:
        cache = f"{read} from cache"
    elif wrote:
        cache = f"{wrote} written to cache"
    elif total < config.CACHE_MIN_TOKENS:
        cache = f"not cached: under the {config.CACHE_MIN_TOKENS}-token minimum"
    else:
        cache = "not cached"
    print(f"  [{tag}] {total} tokens in ({cache}), {usage.output_tokens} out")


def ask_claude(command, workspaces):
    global _ai_error_count
    with _lock:
        messages = CHAT_HISTORY + [{"role": "user", "content": command}]
        # The action list never changes between calls and memory rarely does, so both are
        # cached; everything that changes per call (time, agenda, Stage panels, coding
        # project, answer length) goes after the last marker.
        live = (agenda.context() + _stage_context() + "".join(p() for p in context_providers)
                + f"\n\nREPLY LENGTH (chat replies and any spoken text): {verbosity.rule()}")
        system = [{"type": "text", "text": _system_prompt, "cache_control": config.CACHE_CONTROL}]
        known = memory.prompt_block()
        if known:
            system.append({"type": "text", "text": known, "cache_control": config.CACHE_CONTROL})
        system.append({"type": "text", "text": live.strip()})
    try:
        response = config.client.messages.create(
            model=config.MODEL,
            max_tokens=verbosity.tokens(400, 900),
            system=system,
            messages=messages,
        )
        log_usage("brain", response.usage)
        raw = response.content[0].text.strip()
    except Exception as e:
        print(f"  Unexpected AI error: {e}")
        return {"mode": "none"}

    result = _parse(raw)
    if isinstance(result, dict):
        _ai_error_count = 0
        with _lock:
            CHAT_HISTORY.append({"role": "user", "content": command})
            CHAT_HISTORY.append({"role": "assistant", "content": raw})
            _trim()
            _save_history()
        return result

    print(f"  Could not parse AI response: {raw[:100]}")
    _ai_error_count += 1
    if _ai_error_count >= _AI_ERROR_RESET_THRESHOLD:
        print(f"  {_ai_error_count} consecutive AI errors — resetting chat history.")
        init_chat_history(workspaces)
        _ai_error_count = 0
    return {"mode": "none"}
