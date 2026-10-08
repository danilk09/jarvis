"""
Command dispatch: sends the transcript plus recent history to Claude and parses
the single JSON object it returns ({"mode": "action"|"chat"|"none", ...}).
"""

import json
import threading

from . import config, registry, stage

CHAT_HISTORY: list = []   # user/assistant turns only; the system prompt is kept separately
_system_prompt = ""
_lock = threading.Lock()
_MAX_TURNS = 12           # messages kept in history (~6 exchanges) to bound cost

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
- Any question or request for information that doesn't need real-time data → mode "chat" with a concise spoken reply
- Unclear/filler → mode "none"
- Keep chat replies SHORT: 1-2 sentences max. No markdown, no lists. Plain spoken sentences only. Answer only what was asked — no extra context unless the user asks to go in depth.
"""


def init_chat_history(workspaces):
    global _system_prompt
    with _lock:
        _system_prompt = build_system_prompt(workspaces)
        CHAT_HISTORY.clear()


def remember(user_text, assistant_text=None):
    """Record a side-channel exchange (image analysis, web search, ...) for follow-up questions."""
    with _lock:
        CHAT_HISTORY.append({"role": "user", "content": user_text})
        if assistant_text is not None:
            CHAT_HISTORY.append({"role": "assistant", "content": assistant_text})
        _trim()


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


def ask_claude(command, workspaces):
    global _ai_error_count
    with _lock:
        messages = CHAT_HISTORY + [{"role": "user", "content": command}]
        system   = _system_prompt + _stage_context() + "".join(p() for p in context_providers)
    try:
        response = config.client.messages.create(
            model=config.MODEL,
            max_tokens=400,
            system=system,
            messages=messages,
        )
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
        return result

    print(f"  Could not parse AI response: {raw[:100]}")
    _ai_error_count += 1
    if _ai_error_count >= _AI_ERROR_RESET_THRESHOLD:
        print(f"  {_ai_error_count} consecutive AI errors — resetting chat history.")
        init_chat_history(workspaces)
        _ai_error_count = 0
    return {"mode": "none"}
