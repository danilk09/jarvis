"""
Content generation: single files (generate_file) and project skeletons (coding_mode).
"""

import json
import os
import re

from . import config

_LANG_HINTS = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".html": "HTML", ".css": "CSS", ".md": "Markdown",
    ".json": "JSON", ".sh": "Shell script", ".txt": "plain text",
}

_SKELETON_SYSTEM = (
    'You are a project scaffolding assistant. Output ONLY a raw JSON object with this exact structure:\n'
    '{"project_name":"<short_snake_case_name>","files":[{"path":"<relative_path>","content":"<file_content>"}],"summary":"<1 spoken sentence>"}\n'
    'CRITICAL JSON RULES: All string values must use \\n for newlines, \\t for tabs, \\\\ for backslashes, \\" for quotes. Never use literal newlines inside string values.\n'
    'Framework preference: If the project is a web application, website, frontend, UI, dashboard, or anything browser-based, scaffold it as a React app using Vite (npm create vite). Include src/App.jsx, src/main.jsx, index.html, package.json with react + vite devDependencies, and an install.bat running "npm install". Do NOT use plain HTML/CSS/JS for web projects.\n'
    'Always include:\n'
    '- A main entry point file with skeleton code and TODO comments marking what needs implementing\n'
    '- A requirements.txt (Python) or package.json (JS/TS) listing all needed dependencies\n'
    '- An install.bat that installs all dependencies in one command (e.g. pip install -r requirements.txt or npm install)\n'
    '- A CLAUDE.md describing the project goal, file structure, and what still needs to be implemented\n'
    '- A .gitignore appropriate for the project type. Always exclude: .env, .env.*, *.env, .vscode/, .idea/, *.log, *.tmp. For Python projects also exclude: __pycache__/, *.pyc, *.pyo, venv/, .venv/, dist/, *.egg-info/. For Node/JS projects also exclude: node_modules/, dist/, .next/, .cache/, coverage/.\n'
    'Keep file content minimal — stubs with clear TODO comments, not full implementations.\n'
    'No markdown fences. Output raw JSON only.'
)


def _with_context(prompt_text, context):
    return f"Real-time web data:\n{context}\n\nTask: {prompt_text}" if context else prompt_text


def generate_file_content(prompt_text, filename, context=""):
    """Ask Claude for raw file content, optionally grounded in search/file context."""
    lang_hint = _LANG_HINTS.get(os.path.splitext(filename)[1].lower(), "plain text")
    try:
        resp = config.client.messages.create(
            model=config.MODEL,
            max_tokens=3000,
            system=(
                f"You are a file generator. Output ONLY the raw file content with no "
                f"explanation, preamble, or markdown code fences. "
                f"The file is named '{filename}' and should be {lang_hint}. "
                f"Provide thorough, in-depth content using any real-time data supplied."
            ),
            messages=[{"role": "user", "content": _with_context(prompt_text, context)}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"# File generation failed: {e}"


def summarize_for_speech(content, filename):
    """1-2 spoken sentences about a generated file."""
    try:
        resp = config.client.messages.create(
            model=config.MODEL,
            max_tokens=120,
            system="You are a voice assistant. In 1-2 spoken sentences, briefly summarize what was written. Be concise and conversational. No markdown or bullet points.",
            messages=[{"role": "user", "content": f"Summarize what is in '{filename}':\n\n{content[:2500]}"}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return f"{filename} is ready."


def _repair_json_strings(raw):
    """Escape literal newlines/tabs inside JSON string values so json.loads won't fail."""
    out, in_string, i = [], False, 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and in_string:
            out.append(raw[i:i + 2])
            i += 2
            continue
        if c == '"':
            in_string = not in_string
            out.append(c)
        elif in_string and c in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[c])
        else:
            out.append(c)
        i += 1
    return "".join(out)


def generate_coding_skeleton(prompt_text, context=""):
    """Ask Claude for a project skeleton as {"project_name", "files": [...], "summary"}."""
    try:
        resp = config.client.messages.create(
            model=config.MODEL,
            max_tokens=8192,
            system=_SKELETON_SYSTEM,
            messages=[{"role": "user", "content": _with_context(prompt_text, context)}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[^\n]*\n", "", raw)
            raw = re.sub(r"\n```$", "", raw.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return json.loads(_repair_json_strings(raw))
    except Exception as e:
        print(f"  Skeleton generation error: {e}")
        return None
