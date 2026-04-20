# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running Jarvis

```bash
python jarvis.py
```

Requires a `.env` file with:
```
ANTHROPIC_API_KEY=sk-ant-...
BRAVE_API_KEY=BSA...        # optional — enables real-time web search
```

## Dependencies

The setup script (`setup.sh`) is outdated. The actual required packages are:

```bash
pip install anthropic python-dotenv pyautogui pygetwindow Pillow sounddevice numpy faster-whisper soundfile vosk
```

Optional (for image upscaling): download `realesrgan-ncnn-vulkan.exe` to `C:\tools\realesrgan\` from the Real-ESRGAN-ncnn-vulkan releases page and update `ESRGAN_EXE` in `jarvis.py`.

## Architecture

Everything lives in a single file: `jarvis.py`. The flow is:

1. **Wake word** — Vosk model (`models/vosk-model-small-en-us-0.15/`) listens on a raw audio stream for "jarvis"
2. **Command recording** — A persistent `sounddevice` stream captures audio until silence; faster-whisper transcribes it
3. **AI dispatch** — `ask_claude()` sends the transcript + `CHAT_HISTORY` to Claude Haiku and expects a JSON response with `mode` and optional `actions`
4. **Action execution** — `execute_action()` / `handle_response()` routes the parsed JSON to the appropriate handler (open app, browser, file search, screenshot, image analysis, music, workspace, etc.)
5. **TTS** — A background thread drains `_tts_queue` and speaks via PowerShell's SAPI

**Key globals:**
- `CHAT_HISTORY` — conversation context sent to Claude on every call; guarded by `_chat_lock`
- `FILE_INDEX` / `BOOKMARKS` / `HISTORY` — built in background threads at startup from the local filesystem and browser profiles
- `_audio_buffer` / `_capture_active` — shared between the persistent audio callback and `listen_for_command()`

## Workspaces

Workspaces are defined in `workspaces.json`. Each workspace is a named list of items with types `url`, `vscode`, `file`, or `app`. The `workspace_manager.html` file is a browser-based UI for editing this file.

## Configuration (top of jarvis.py)

| Variable | Purpose |
|---|---|
| `VOICE_NAME` | SAPI voice name fragment (empty = system default) |
| `SPEECH_RATE` | TTS rate, -10 to 10 |
| `DEFAULT_PLAYLIST_URL` | Amazon Music fallback playlist |
| `WAKE_WORD` | Defaults to `"jarvis"` |

## Claude API usage

The project uses `claude-haiku-4-5-20251001` for both the main command dispatch (`ask_claude`) and image analysis (`analyze_image_with_claude`, `_pillow_enhance`). The system prompt instructs the model to return only a single JSON object. The `max_tokens` is capped at 400 to keep latency low. History is trimmed to 13 messages (system prompt + 12 turns) to control cost.
