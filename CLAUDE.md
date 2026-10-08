# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running Jarvis

```bash
python jarvis.py                # development mode (console output, opens the dashboard)
python jarvis.py --background   # 24/7 mode — see "Background mode" below
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
pip install anthropic python-dotenv Pillow \
    sounddevice numpy faster-whisper vosk requests \
    flask flask-cors webrtcvad "yt-dlp[default]" pycaw comtypes pypdf
```

Optional (for image upscaling): download `realesrgan-ncnn-vulkan.exe` from the Real-ESRGAN-ncnn-vulkan releases page and update `ESRGAN_EXE` in `core/config.py`.

Music playback also needs a JavaScript runtime for yt-dlp to solve YouTube's challenges (Node or deno on PATH — without one, streams 403 after the first ~0.5 MB). Keep yt-dlp current (`pip install -U "yt-dlp[default]"`); YouTube breaks old versions regularly.

Optional (for music playback): download mpv from mpv.io (shinchiro Windows build, `mpv-x86_64-*.7z`) and add the folder to PATH, or set `MPV_EXE` in `core/config.py` to the full path.

## Architecture

`jarvis.py` is only startup + the main loop. Everything else is in `core/`:

| Module | Responsibility |
|---|---|
| `config.py` | All settings, paths, the Anthropic client. Edit settings here. |
| `state.py` | Shared state: dashboard status (`dash_state`), `workspaces` (mutated in place for hot-reload), session archive path |
| `tts.py` | In-process SAPI speech via `comtypes` on one dedicated thread. `speak()`, `stop_tts()`, `wait_tts()`, `suppressed` |
| `audio.py` | One shared 16 kHz mic stream → 1 s pre-roll ring, wake-word thread (Vosk, grammar-limited to "jarvis"), and the command recorder (WebRTC VAD). Whisper transcription. |
| `router.py` | Local fast path: music controls, "play X", "open <app>", time/date, timers — handled without calling Claude |
| `brain.py` | `ask_claude()` — system prompt (built from the registry) + `CHAT_HISTORY` → one JSON object |
| `registry.py` | `@action(name, schema, rules)` decorator; schemas/rules are compiled into the system prompt |
| `actions.py` | All built-in action handlers + `handle_response()` |
| `agent.py` | "Do anything" fallback: tool-use loop (PowerShell, files, web, Claude Code hand-off) with spoken confirmation for anything non-read-only |
| `music.py` | yt-dlp (in-process) resolves, audio is fetched in range chunks and piped to mpv's stdin; YouTube Mix autoplay with next-track prefetch; ducking via mpv IPC / pycaw |
| `files.py` | File index, bookmarks, Start-menu app index, workspaces |
| `media.py` | Screenshots, image analysis/enhancement, `jarvis_input/` processing |
| `generate.py` | `generate_file` content and `coding_mode` project skeletons |
| `web.py` | Brave search + spoken summary, page fetching |
| `server.py` | Flask: dashboard, JSON API, `/phone` page (`core/static/phone.html`), Tailscale HTTPS |
| `background.py` | `--background` only: log file, periodic index refresh |

Flow of one command:

1. **Wake word** — `audio._wake_loop` feeds Vosk 100 ms batches while `audio.enable_wake()` is on. On "jarvis" it immediately runs `audio.on_wake` callbacks (`stop_tts`, `music.duck`), starts recording, and sets `audio.activated`.
2. **Command** — `audio.capture_followup()` checks whether the user kept talking ("Jarvis, open Discord"); if not, Jarvis says "Yes sir" and `audio.listen_for_command()` records until silence. faster-whisper transcribes from memory.
3. **Routing** — `router.route()` handles common commands locally; otherwise `brain.ask_claude()` returns `{"mode": "action"|"chat"|"none", ...}`.
4. **Execution** — `actions.handle_response()` looks each action's `type` up in `registry.ACTIONS`. Wake listening is re-enabled during execution so the user can interrupt.
5. **TTS** — the TTS thread speaks queued text; real SAPI word positions drive the dashboard's `speech_beat`.

**Adding a command:** write a handler in `core/actions.py` decorated with `@action("type", schema='{"type":"type",...}', rules=['"trigger phrase" → type "type"'])`. It appears in Claude's system prompt automatically. If it's common and unambiguous, also add a pattern to `core/router.py` so it skips the API.

## Dashboard

A React app lives in `jarvis-dashboard/`. It is built with Create React App and served by the embedded Flask server at `http://localhost:5151`.

- `/` — live status orb, transcript display, generated-files panel
- `/workspaces` — React workspace manager; reads/writes `workspaces.json` via the Flask API
- `/phone` — tap-to-speak mobile page (`core/static/phone.html`, not part of the React build)

To rebuild after frontend changes:
```bash
cd jarvis-dashboard && npm run build
```

Flask API routes relevant to the dashboard:
- `GET /status?files=<n>` — current state, transcript, conversation `log`, speech beat, current `speech` (id/start time), music state, and the file list (omitted when the client already has `n` files)
- `GET /api/speech/<id>` — 16-band spectral envelope of one utterance (computed in `tts._envelope`); the orb replays it in sync with the audio
- `POST /api/activate` — same as saying the wake word (clicking the orb)
- `GET /api/workspaces` — returns the contents of `workspaces.json`
- `POST /api/workspaces` — writes `workspaces.json`, updates `state.workspaces`, and rebuilds the system prompt

## Workspaces

Workspaces are defined in `workspaces.json`. Each workspace is a named object with an optional `description` and an `items` array. Each item has a `type` (`url`, `vscode`, `file`, or `app`) and a `path`.

Edit them at `http://localhost:5151/workspaces`. Changes save instantly and hot-reload the running assistant's voice triggers.

## Configuration (`core/config.py`)

| Variable | Purpose |
|---|---|
| `VOICE_NAME` | SAPI voice name fragment, e.g. `"Zira"` (empty = system default) |
| `SPEECH_RATE` | TTS rate, -10 to 10 |
| `WAKE_WORD` | Defaults to `"jarvis"` |
| `WAKE_GRAMMAR` | Restrict Vosk to the wake word (less CPU, fewer false hits). Set `False` if detection gets unreliable |
| `INPUT_DEVICE` | Mic name fragment to pin (e.g. `"AirPods"`); empty = follow the Windows default, switching automatically when it changes |
| `FOLLOWUP_WINDOW` | Seconds to wait after the wake word for a one-breath command before saying "Yes sir" |
| `MPV_EXE` / `ESRGAN_EXE` | Paths to optional external tools |

## Background mode

Built but **not in use yet** — Jarvis is still in development. `python jarvis.py --background` skips opening the browser, sends all output to `logs/jarvis.log` (rotated), and refreshes the file/bookmark/app indexes hourly. `scripts/install_startup.ps1` registers that command (via `pythonw.exe`, no console) as a login task; `scripts/uninstall_startup.ps1` removes it. Do not install the startup task unless asked.

## Claude API usage

The project uses `claude-haiku-4-5-20251001` (`config.MODEL`) for all Claude calls:
- `brain.ask_claude` — main command dispatch; returns a single JSON object; `max_tokens=400`
- `agent.run_agent` — tool-use loop for tasks no action covers; up to 8 steps, `max_tokens=1500` per step
- `media.analyze_image` — image analysis with concise spoken answers; `max_tokens=200`
- `media._pillow_enhance` — derives image enhancement params as JSON; `max_tokens=200`

History keeps the last 12 messages, always starting on a user turn. Side results (image analysis, web search, input-folder contents, agent results) are added with `brain.remember()`. Prompt caching does not apply: Haiku 4.5's minimum cacheable prefix is 4096 tokens and the system prompt is ~1.4k.
