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

Run `setup.sh` for a full automated setup (installs packages, downloads the Vosk model, creates folders and a `.env` template):

```bash
bash setup.sh
```

Required packages (installed by setup.sh):

```bash
pip install anthropic python-dotenv pyautogui pygetwindow Pillow \
    sounddevice numpy faster-whisper soundfile vosk requests \
    flask flask-cors webrtcvad yt-dlp pycaw
```

Optional (for image upscaling): download `realesrgan-ncnn-vulkan.exe` from the Real-ESRGAN-ncnn-vulkan releases page and update `ESRGAN_EXE` in `jarvis.py`.

Optional (for music playback): download mpv from mpv.io (shinchiro Windows build, `mpv-x86_64-*.7z`) and add the folder to PATH, or set `MPV_EXE` in `jarvis.py` to the full path.

## Architecture

Everything lives in a single file: `jarvis.py`. The flow is:

1. **Wake word** — Vosk model (`models/vosk-model-small-en-us-0.15/`) listens on a raw audio stream for "jarvis"
2. **Command recording** — A persistent `sounddevice` stream captures audio until silence; faster-whisper transcribes it
3. **AI dispatch** — `ask_claude()` sends the transcript + `CHAT_HISTORY` to Claude Haiku and expects a JSON response with `mode` and optional `actions`
4. **Action execution** — `execute_action()` / `handle_response()` routes the parsed JSON to the appropriate handler (open app, browser, file search, screenshot, image analysis, workspace, coding mode, music, etc.)
5. **TTS** — A background thread drains `_tts_queue` and speaks via PowerShell's SAPI. TTS can be interrupted mid-speech by saying the wake word; `stop_tts()` kills the current subprocess and drains the queue.
6. **Music** — `play_music()` uses yt-dlp to resolve a YouTube audio stream URL and passes it to mpv. Volume is controlled via mpv's named-pipe IPC (`\\.\pipe\jarvis_mpv`) using ctypes. When the wake word fires, `music_duck()` fades volume to 8% over 0.5s; after Jarvis finishes speaking, `music_unduck()` fades back to 100% over 1.5s. Concurrent fades are cancelled safely using a generation counter (`_fade_gen`).

**Key globals:**
- `CHAT_HISTORY` — conversation context sent to Claude on every call; guarded by `_chat_lock`
- `_workspaces` — workspace config dict; updated live by the `POST /api/workspaces` endpoint so voice triggers reload without restarting
- `FILE_INDEX` / `BOOKMARKS` — built in background threads at startup from the local filesystem and Chrome/Edge bookmark files
- `_audio_buffer` / `_capture_active` — shared between the persistent audio callback and `listen_for_command()`
- `_tts_suppressed` — set when TTS is interrupted; cleared at the start of each new activation cycle so subsequent `speak()` calls work normally
- `_music_proc` / `_music_vol` / `_fade_gen` — current mpv subprocess, tracked volume level, and fade generation counter for the music subsystem

## Dashboard

A React app lives in `jarvis-dashboard/`. It is built with Create React App and served by the embedded Flask server at `http://localhost:5151`.

- `/` — live status orb, transcript display, generated-files panel
- `/workspaces` — React workspace manager; reads/writes `workspaces.json` via the Flask API
- `/phone` — tap-to-speak mobile page (served as plain HTML, not from the React build)

To rebuild after frontend changes:
```bash
cd jarvis-dashboard && npm run build
```

Flask API routes relevant to the dashboard:
- `GET /status` — returns current state, transcript, speech beat, and file list
- `GET /api/workspaces` — returns the contents of `workspaces.json`
- `POST /api/workspaces` — writes `workspaces.json`, updates `_workspaces`, and re-initializes `CHAT_HISTORY`

## Workspaces

Workspaces are defined in `workspaces.json`. Each workspace is a named object with an optional `description` and an `items` array. Each item has a `type` (`url`, `vscode`, `file`, or `app`) and a `path`.

Edit them at `http://localhost:5151/workspaces`. Changes save instantly and hot-reload the running assistant's voice triggers.

## Configuration (top of jarvis.py)

| Variable | Purpose |
|---|---|
| `VOICE_NAME` | SAPI voice name fragment (empty = system default) |
| `SPEECH_RATE` | TTS rate, -10 to 10 |
| `WAKE_WORD` | Defaults to `"jarvis"` |
| `MPV_EXE` | Path to mpv executable (default `"mpv"` — must be on PATH or set to full path) |

## Claude API usage

The project uses `claude-haiku-4-5-20251001` for all Claude calls:
- `ask_claude` — main command dispatch; returns a single JSON object; `max_tokens=400`
- `analyze_image_with_claude` — image analysis with a system prompt enforcing concise spoken answers; `max_tokens=200`
- `_pillow_enhance` — derives image enhancement params as JSON; `max_tokens=200`

History is trimmed to 13 messages (system prompt + 12 turns) to control cost.
