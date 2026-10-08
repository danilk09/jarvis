"""
Content generation for generate_file (coding projects are built by Claude Code, see coding.py).
"""

import os

from . import config

_LANG_HINTS = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".html": "HTML", ".css": "CSS", ".md": "Markdown",
    ".json": "JSON", ".sh": "Shell script", ".txt": "plain text",
}

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
