# JARVIS

A voice-activated AI assistant for Windows. Say "Jarvis", speak your command, and it figures out what to do — open apps, search the web, analyze screenshots, scaffold code, etc.

## How it works

1. **Wake word** — Vosk listens offline for "jarvis" on a continuous audio stream
2. **Command recording** — faster-whisper transcribes your speech after the wake word
3. **AI dispatch** — Claude Haiku parses the transcript and returns a JSON action object
4. **Execution** — the action is routed to the appropriate handler
5. **TTS** — response is spoken via Windows SAPI; saying the wake word mid-speech interrupts it

## Setup

```bash
bash setup.sh
```

This installs dependencies, downloads the Vosk model, creates folders, and writes a `.env` template.

Then fill in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
BRAVE_API_KEY=BSA...        # optional — enables real-time web search
```

Run:

```bash
python jarvis.py
```

## What you can say

| Command type | Example |
|---|---|
| Open a workspace | "Open my coding workspace" |
| Open a URL | "Open github" |
| Launch an app | "Open Spotify" |
| Search the web | "Search YouTube for lo-fi beats" |
| Find and open files | "Find files named resume" |
| Open a bookmark | "Open my Notion bookmark" |
| Take and analyze a screenshot | "Take a screenshot and tell me what this place is" |
| Analyze an image | "What's in my input folder?" |
| Web search (live) | "What's the weather in New York?" |
| Generate a file | "Create a file about the history of programming" |
| Scaffold a project | "Code hello world" |
| Play music | "Play Bohemian Rhapsody" / "Play some lo-fi beats" |
| Stop / pause music | "Stop the music" / "Pause music" |
| Chat | "What is the difference between a thread and process?" |

## Features

**Voice pipeline**
- Offline wake word detection with Vosk (no cloud round-trip)
- WebRTC VAD for noise-resistant silence detection
- Interrupt TTS mid-sentence by saying the wake word again

**Music**
- Play any song or artist by voice — yt-dlp resolves a YouTube audio stream, mpv plays it with no download
- Music automatically ducks (fades to ~8%) when the wake word is heard and fades back to full volume after Jarvis finishes speaking
- Supports play, pause, resume, and stop commands

**Dashboard**
- React UI at `http://localhost:5151` showing live state, transcript, and generated files

**Phone interface**
- Tap-to-speak web page served over Tailscale HTTPS — send commands from your phone without being near your PC

**Workspaces**
- Named groups of URLs, apps, VS Code projects, and files defined in `workspaces.json`
- Edit them via the React workspace manager at `http://localhost:5151/workspaces` — saves directly to `workspaces.json` and hot-reloads voice triggers without restarting

**File & browser integration**
- Indexes local files at startup for fast fuzzy search
- Reads bookmarks from Chrome/Edge profiles

**Image I/O**
- Drop images into `jarvis_input/` and ask Jarvis to analyze them
- Generated images and files go to `jarvis_output/`

## Dependencies

```
anthropic python-dotenv pyautogui pygetwindow Pillow
sounddevice numpy faster-whisper soundfile vosk
requests flask flask-cors webrtcvad yt-dlp
```

Optional: `realesrgan-ncnn-vulkan.exe` for image upscaling (update `ESRGAN_EXE` in `jarvis.py`).

Optional (music): [mpv](https://mpv.io) — download the shinchiro Windows build (`mpv-x86_64-*.7z`), extract it, and add the folder to PATH (or set `MPV_EXE` in `jarvis.py`).
