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
GITHUB_TOKEN=github_pat_... # optional — private repos + GitHub MCP for Claude Code (scripts/setup_github_mcp.py)
WOLFRAM_APP_ID=...          # optional — exact math/science answers (developer.wolframalpha.com)
POSTGRES_URL=postgresql://... # optional — read-only Postgres MCP for Claude Code
POSTMAN_API_KEY=PMAK-...    # optional — else Postman signs in through the browser
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
    flask flask-cors webrtcvad "yt-dlp[default]" pycaw comtypes pypdf trafilatura sherpa-onnx uv
```

MCP servers: `python scripts/setup_mcp.py status` shows what's set up; `google` (reads which tools your claude.ai Gmail/Drive/Calendar connectors have → `jarvis_memory/mcp/google_tools.json`), `coding` (Context7, Playwright, Postman, Firebase, Docker, Postgres → Claude Code user scope), `jobs` (downloads JobSpy), `miro` (sign-in). Python-based servers run through `uvx --python 3.12` (they don't support 3.14); `delegate.uvx_exe()` also finds uvx in Python's per-user Scripts folder.

The desktop app (needs Node.js): `cd desktop && npm install` — if `node_modules/electron/dist/electron.exe` is missing afterwards, run `node node_modules/electron/install.js`. Without it Jarvis opens the dashboard in the browser and Stage pages fall back to a reader view.

Optional (for image upscaling): download `realesrgan-ncnn-vulkan.exe` from the Real-ESRGAN-ncnn-vulkan releases page and update `ESRGAN_EXE` in `core/config.py`.

Music playback also needs a JavaScript runtime for yt-dlp to solve YouTube's challenges (Node or deno on PATH — without one, streams 403 after the first ~0.5 MB). Keep yt-dlp current (`pip install -U "yt-dlp[default]"`); YouTube breaks old versions regularly.

Optional (for music playback): download mpv from mpv.io (shinchiro Windows build, `mpv-x86_64-*.7z`) and add the folder to PATH, or set `MPV_EXE` in `core/config.py` to the full path.

## Architecture

`jarvis.py` is only startup + the main loop. Everything else is in `core/`:

| Module | Responsibility |
|---|---|
| `config.py` | All settings, paths, the Anthropic client. Edit settings here. |
| `state.py` | Shared state: dashboard status (`dash_state`), `workspaces` (mutated in place for hot-reload), session archive path |
| `tts.py` | Speech on one dedicated thread: a Piper voice (`config.PIPER_VOICE`, offline, via sherpa-onnx) or in-process SAPI via `comtypes` (also the fallback if Piper is missing or fails). Piper lines are split into whole sentences and at dashes (`_phrases`; commas stay inside — rendering comma pieces separately reset the intonation each time), each piece's silence replaced by a set pause; the first plays as soon as it renders (~60-150 ms) while the rest render behind it, into an output stream kept open between lines (`_Player`; opening one costs 100-300 ms; reopened after audio.py resets PortAudio or when `_watch_speakers` sees the default speakers change). Piper's orb beats come from loudness onsets. `speak()`, `stop_tts()`, `wait_tts()`, `suppressed`. Custom pronunciations (`config.PRONUNCIATIONS` + `jarvis_memory/pronunciations.json`, taught by voice with the `pronounce` action) become SAPI XML `<pron sym="…">` tags at speak time only — the display text keeps the spelling; bad phonemes fall back to plain speech. For Piper the phonemes become a sounds-like spelling (`_respelled`: "d ax n iy 1 l" → "duh-neel") |
| `audio.py` | One shared 16 kHz mic stream → 4 s pre-roll ring + 3 s level history (noise floor), wake-word thread (Vosk, grammar: "jarvis" + decoys; `_QuietGate` skips decoding while the room is quiet — 0.4 s pre-roll, 1 s hold — which cut idle CPU without missing any of 85 recorded "Jarvis, …" commands), and the command recorder (WebRTC VAD gated by loudness over the room). Whisper transcription (via `asr.py`). The detector runs behind real time, so `_fire_wake` records its lag and keeps 2.5 s before the point it had reached (`wake_audio()` uses the same offset for voice ID); the false-wake check looks for "Jarvis" anywhere in the transcript, or a close spelling ("Travis", "Darvis") in the first two words, and logs real false wakes to `jarvis_memory/voice/false_wakes.jsonl`. `listen_for_command` (every listen: after "Yes sir", answers, confirmations, voice ID) shows LISTENING, pauses the wake word so "Jarvis"/the orb can't queue a second activation, and keeps `state.listening` set so speech ending can't flip the dashboard to STANDBY. Keeps the last recording in `audio.last_capture` for voice ID. |
| `asr.py` | Whisper settings shared with `scripts/voice/benchmark.py`: hint prompt = fixed command phrases + workspace names + your vocabulary, prompt-echo stripping, learned fixes applied after decoding |
| `speech_data.py` | Voice data in `jarvis_memory/voice/`: every command's audio + transcript, labels (true text), "heard → meant" corrections that become automatic fixes, `vocabulary.txt` |
| `voice_id.py` | Speaker verification: NeMo TitaNet-small (ONNX via sherpa-onnx, ~80 ms on CPU) voiceprints vs. your enrolled profile; modes off / shadow / enforce |
| `voice.py` | Voice ID in the conversation: `screen()` on every command, the "who is this?" challenge, enrollment ("learn my voice"), `voice` action |
| `router.py` | Local fast path: music controls, "play X", "open <app>", time/date, timers, agenda questions, "breakdown of today" and "good morning/afternoon/evening" (→ briefing), "good night"/"shut down" (→ `shutdown`), "thanks", "what do you remember about me" — handled without calling Claude |
| `brain.py` | `ask_claude()` — system prompt (built from the registry) + `CHAT_HISTORY` → one JSON object |
| `registry.py` | `@action(name, schema, rules)` decorator; schemas/rules are compiled into the system prompt |
| `actions.py` | All built-in action handlers + `handle_response()` |
| `memory.py` | Long-term memory: facts in `jarvis_memory/memory.md` (cached prompt block), daily conversation logs, background review that saves facts mentioned in passing, daily summaries, `memory` action (remember / forget / update / show / recall) |
| `checkins.py` | "How did the quiz go yesterday?": events with real stakes (from the agenda, classified once by Haiku, or mentioned in conversation via the memory review) are asked about once, at a natural moment; the answer becomes a `checkin` action (friendly reply + outcome + "Working on" facts). See "Check-ins" below |
| `agenda.py` | Deadlines, appointments, to-dos and timers in `jarvis_memory/agenda.json`; recurring rules; reminder thread; `agenda` action; the NOW + agenda lines in the live prompt block |
| `briefing.py` | "Give me a breakdown of today" or "good morning/afternoon/evening": weather (Open-Meteo), headlines (Brave News), agenda (today and coming up — never anything overdue or yesterday's leftovers), special days, gathered in parallel; one Haiku call writes the spoken version (opens with a holiday greeting, sky events as an aside); full breakdown as a Stage note. Unread email (`BRIEFING_EMAIL`) is checked in parallel by a quiet `delegate` google run (Haiku) and spoken right after the breakdown; a check-in question waits until after it (`checkins.hold`/`release`) |
| `events.py` | Special days: US holidays + fun observances, daylight saving changes, moon phases and seasons (USNO API), meteor shower peaks, eclipses (2026-28), today's rocket launches (Launch Library 2). Upcoming holidays also go into the agenda prompt so Claude resolves "Thanksgiving week" correctly |
| `places.py` | `find_places` ("pizza restaurants in Honolulu"): Brave search → Haiku picks named places with one-line descriptions → located with Photon (parallel) → `places` Stage panel; opening hours/websites filled in afterwards from Overpass |
| `notify.py` | Phone notifications via ntfy: reminders when you're away from the PC (no input/voice for `PHONE_AWAY_MIN`), urgent deadlines always; Mark done / Snooze buttons call `POST /api/notify/action` over Tailscale (secret key in `jarvis_memory/notify.json`); `send_to_phone` action |
| `agent.py` | "Do anything" fallback: tool-use loop (PowerShell, files, web, Claude Code hand-off via `coding.start`) with spoken confirmation for anything non-read-only |
| `music.py` | yt-dlp (in-process) resolves, audio is fetched in range chunks and piped to mpv's stdin; YouTube Mix autoplay with next-track prefetch; ducks while listening and whenever Jarvis speaks (TTS hooks), via mpv IPC volume |
| `files.py` | File index, bookmarks, Start-menu app index, workspaces |
| `media.py` | Screenshots, image analysis/enhancement, `jarvis_input/` processing |
| `generate.py` | `generate_file` content |
| `coding.py` | Coding projects via Claude Code: `code` action (new / run / switch / clone / repos / stop / approve / remember …), any repo in `REPO_DIRS` by spoken name, up to `CODE_MAX_PARALLEL` runs at once, a `jarvis/<task>` branch per task, headless `claude -p --output-format stream-json` runs streamed onto a Stage `code` panel, per-project session resume, voice approval of refused commands, preferences in `jarvis_coding/CLAUDE.md` |
| `web.py` | Brave search + spoken summary, page fetching |
| `delegate.py` | `delegate` action: open-ended tasks needing an outside service go to Claude Code (subscription) with only that service's MCP servers — `SERVICES`: `google` (the claude.ai Gmail/Drive/Calendar connectors — no Google Cloud setup), `jobs` (JobSpy; Indeed connector optional), `miro`, `general` (your normal Claude Code setup). Runs through `coding.start(service=...)` |
| `knowledge.py` | `wolfram` (Spoken Results API → ready-to-say sentence; LLM API + Haiku fallback) and `youtube` (captions via yt-dlp → one Haiku call; transcript saved to `jarvis_output/transcripts/`) |
| `server.py` | Flask: dashboard, JSON API, Stage API + event stream, `/phone` page (`core/static/phone.html`), `/phone-command` (voice) and `/phone-image` (photo + optional typed/spoken question → JPEG ≤1568 px in `jarvis_output/phone/`, queued as `{"text", "image"}`; the main loop sends it to `brain.ask_claude(..., image=)`, skipping the router), Tailscale HTTPS |
| `stage.py` | Stage state (panels, 12×12 layout, highlights) in memory; pushes every change to dashboards over SSE. Auto-tiling, voice references ("panel 2", "the map"), content helpers (`add_image`, `open_file`, `show_places`) |
| `stage_actions.py` | Stage actions: `show_page`, `read_article` (live page + key points, narrated with highlights), `point_out`, `show_file` / `edit_file` / `file_changes`, `show_image`, `show_note`, `show_map` (Nominatim geocoding), `stage` (layout by voice) |
| `desktop.py` | Opens the dashboard in the Electron app (`desktop/`), or the browser if it isn't installed; `close()` on shutdown |
| `background.py` | `--background` only: log file, console programs started hidden (`hide_child_consoles`), periodic index refresh |
| `power.py` | Battery saving: on battery, closes the mic (`audio.pause()`) and the voice's open speaker stream (`tts.release_output()`) when the PC is locked or quiet for `DORMANT_AFTER_MIN`; keyboard/mouse input, unlocking, plugging in or an orb click reopens it. Phone commands, reminders and Claude Code keep working |

Flow of one command:

1. **Wake word** — `audio._wake_loop` feeds Vosk 100 ms batches while `audio.enable_wake()` is on. On "jarvis" it immediately runs `audio.on_wake` callbacks (`stop_tts`, `music.duck`), starts recording, and sets `audio.activated`.
2. **Command** — `audio.capture_followup()` checks whether the user kept talking ("Jarvis, open Discord"); if not, Jarvis says "Yes sir" and `audio.listen_for_command()` records until silence. faster-whisper transcribes from memory.
   **Voice ID** — `voice.screen()` scores the recording against your voiceprint, saves it to `jarvis_memory/voice/`, and (in enforce mode) drops or challenges anyone else. "No, I said X" labels the previous recording, learns the fix and runs X instead.
3. **Routing** — `router.route()` handles common commands locally; otherwise `brain.ask_claude()` returns `{"mode": "action"|"chat"|"none", ...}`.
4. **Execution** — `actions.handle_response()` looks each action's `type` up in `registry.ACTIONS`. Wake listening is re-enabled during execution so the user can interrupt.
5. **TTS** — the TTS thread speaks queued text (Piper voice, else SAPI); SAPI word positions or Piper loudness onsets drive the dashboard's `speech_beat`.
6. **Answers without the wake word** — any spoken line ending in "?" is recorded in `tts.last_question`. When a turn ends (or, while idle, when a background job like Claude Code asks), if nothing has listened since (`audio.last_listen`), `jarvis._listen_for_answer` listens for `ANSWER_WINDOW` seconds and sends the reply on as `(Answering your question "...") <answer>`. Up to 3 in a row; "never mind" or silence ends it. Handlers that ask and listen themselves (`agent.confirm`) aren't asked twice. Claude is told to end with a question only when it needs the answer.

**Adding a command:** write a handler in `core/actions.py` decorated with `@action("type", schema='{"type":"type",...}', rules=['"trigger phrase" → type "type"'])`. It appears in Claude's system prompt automatically. If it's common and unambiguous, also add a pattern to `core/router.py` so it skips the API.

## Dashboard

A React app lives in `jarvis-dashboard/`. It is built with Create React App and served by the embedded Flask server at `http://localhost:5151`.

- `/` — live status orb, transcript display, generated-files panel
- `/stage` — the Stage (see below); always mounted so live pages keep their state on other tabs
- `/workspaces` — React workspace manager; reads/writes `workspaces.json` via the Flask API
- `/phone` — mobile page (`core/static/phone.html`, not part of the React build): tap to speak, CAMERA / PHOTOS (shrunk to a JPEG in the browser, then a typed or spoken question), and Jarvis's reply read from `/status`'s log (tap it to hear it)

To rebuild after frontend changes:
```bash
cd jarvis-dashboard && npm run build
```

Flask API routes relevant to the dashboard:
- `GET /status?files=<n>` — current state, transcript, conversation `log`, speech beat, current `speech` (id/start time), music state, and the file list (omitted when the client already has `n` files)
- `GET /api/speech/<id>` — 16-band spectral envelope of one utterance (computed in `tts._envelope`); the orb replays it in sync with the audio
- `POST /api/activate` — same as saying the wake word (clicking the orb)
- `POST /api/voice/label` — `{sample, text}` corrects what Jarvis heard, `{sample, text, confirmed: true}` marks it right (the ✎ / ✓ on your lines in the Activity feed)
- `GET /api/workspaces` — returns the contents of `workspaces.json`
- `POST /api/workspaces` — writes `workspaces.json`, updates `state.workspaces`, and rebuilds the system prompt

## Stage

`/stage` is a live canvas Jarvis fills while it works; the dashboard switches to it automatically when a panel opens (and opens the desktop app if no window is connected).

- **Saved between runs** — every change is written (debounced) to `stage_state.json` (`config.STAGE_STATE_FILE`, git-ignored) and `stage.load()` restores it at startup: file panels reload their contents from disk, image/file panels whose file is gone are dropped. "Clear the stage" resets it. Closing panels, clearing, and auto-closing past `MAX_PANELS` are undoable (`stage.restore_removed()`, voice "restore the stage", the Undo button), and the undo survives restarts too.

- **Panel kinds** — `page` (live web page: an Electron `<webview>` in the desktop app, reader view in a browser), `summary` (key points, each linked to a quote in its page), `file` (Monaco editor, autosaves to `jarvis_output/`; Jarvis's edits arrive as a diff to accept/reject), `image`, `note` (Markdown; double-click or Edit to change it by hand), `map` (CesiumJS globe, loaded from the jsDelivr CDN on first use), `places` (numbered options beside a Leaflet street map with Esri's keyless dark tiles — Leaflet from jsDelivr, wrapped like Cesium so Monaco's AMD `define` doesn't swallow it; clicking an option or pin highlights `{"place": i}`).
- **Layout** — 12×12 grid filling the screen. `auto` splits the screen recursively by each kind's weight (`stage.KIND_WEIGHT`); `focus` and `columns` too. Drag by the header (drop on another panel to swap), resize from edges, or by voice. Manual edits make the layout `custom` until the next arrange.
- **Highlights** — `stage.highlight(panel, target, label)`: `{"quote"}` (page/note/summary, found and marked inside the page by `pageScripts.ts`), `{"region": [x,y,w,h]}` (image, fractions), `{"lines": [a,b]}` (file), `{"place": i}` (map). `speak(text, on_start=...)` fires a highlight the moment that sentence starts, and a beam is drawn from the Stage orb to the mark.
- **Frontend** — `jarvis-dashboard/src/stage/`: `StageContext.tsx` (SSE), `StagePage.tsx` (react-grid-layout), `panels/*`, `StageBeam.tsx`, `anchors.ts`. `pageScripts.ts` functions are injected into live pages via `toString()`, so they must stay plain ES5 with no outside references.
- **Desktop app** — `desktop/main.js`: loads the dashboard, enables `<webview>` (locked down: no preload, sandboxed, http(s) only), blocks ads/cookie banners in Stage pages, denies location and other permissions, opens dashboard links externally. Dev aid: `JARVIS_CAPTURE=out.png JARVIS_CAPTURE_DELAY=ms [JARVIS_CAPTURE_EVAL=js]` screenshots (and prints the JS result) and exits.

Flask API: `GET /api/stage/events` (SSE), `POST /api/stage/layout | arrange | clear | restore | highlight | highlights/clear`, `POST /api/stage/panel/<id>/close`, `GET /api/stage/media/<id>` (image panels only — files Jarvis put there), `POST /api/stage/file/<id>` (save / accept / reject), `POST /api/stage/note/<id>` (note edited by hand), `POST /api/stage/page/<id>` (live page navigated), `POST /api/stage/extract/<request>` (text of a live page), `GET /api/stage/config`.

## Coding projects (Claude Code)

`core/coding.py`. "Code me X" creates `jarvis_coding/<name>/` and Claude Code builds it; later requests ("add dark mode") go straight to Claude Code in the **current project**, resuming that project's session (`--resume <session id>`) so it keeps context. State (current project, session ids, pending approvals) is in `coding_state.json` (git-ignored).

- **Runs headless** (`claude -p --output-format stream-json --verbose`), prompt on stdin, launched via the real `claude.exe` (npm's `.cmd` shim is unwrapped by `coding.claude_exe()`). Events stream onto a Stage `code` panel; Claude Code's closing summary is spoken (it's told to end with one via `--append-system-prompt`).
- **Permissions** — `CODE_PERMISSION_MODE = "acceptEdits"`: file edits and file operations inside the project are automatic; shell commands must match `CODE_ALLOWED_TOOLS` (`PowerShell(...)` and `Bash(...)` prefix rules — Claude Code's shell tool on this machine is PowerShell). Refused commands come back as `permission_denials`; Jarvis asks, and "allow it" (`coding.approve()`) resumes with each part of the command allowed. Claude Code's PowerShell permission parser can time out and refuse a command that then succeeds on retry, so only denials that never succeeded are reported.
- **Voice → settings** — the `code` action carries `effort` (`--effort`), `model` (`--model`) and `plan_only` (`--permission-mode plan`; "go ahead" afterwards runs "Implement the plan you proposed."). Subagents aren't a flag: the user's wording stays in the request. Defaults: `CODE_EFFORT`, `CODE_MODEL` in `config.py`.
- **Preferences** — `jarvis_coding/CLAUDE.md`. Claude Code loads CLAUDE.md from the project folder and every parent, so this applies to every project. "Remember I prefer X" appends a line (`coding.remember_pref`); "show my coding preferences" opens it on the Stage.
- **Your repos** — every folder in `REPO_DIRS` (default `Desktop/GitHub`) is a project: "work on murphys next js" matches `murphys-nextjs` (`_find_project`: exact ignoring separators, then all words, shortest wins, then closest spelling). "Clone my X repo" clones from `github.com/GITHUB_USER` (private repos need `GITHUB_TOKEN` in `.env`). "Which repos have changes" → `repo_status()` note on the Stage.
- **Parallel runs** — `_jobs` holds one run per project, up to `CODE_MAX_PARALLEL` (3). Each has its own Stage `code` panel; finishes are announced by project name. stop / approve / VS Code / terminal take a project (panel buttons send theirs); approve defaults to the current project, else the one that asked last.
- **Branch per task** (`CODE_BRANCH_PER_TASK`) — before a run in a git repo, `_prepare_branch` creates `jarvis/<task-slug>` from the current branch (asking first if there are uncommitted changes); follow-ups stay on a `jarvis/` branch unless `fresh_branch`. Claude Code is told to commit there and not push/merge/switch; `git push` isn't allowed, so it comes back as a denial and Jarvis asks. Plans and `question` runs (read-only questions like "any new issues on hiclimb", run in plan mode at low effort) don't branch.
- **GitHub** — `scripts/setup_github_mcp.py` registers GitHub's MCP server (`https://api.githubcopilot.com/mcp/`, user scope) with the token from `.env`. Read tools (`mcp__github__get_*`, `list_*`, `search_*`…) are in `CODE_ALLOWED_TOOLS`; anything that writes (open PR, comment, merge) is denied and approved by voice — `approve()` allows the tool itself.
- **Phone** — headless runs can't be watched through Remote Control (it only works in interactive sessions, and `--remote-control` is silently ignored with `-p`). Instead: "continue this on my phone" (`open_on_phone`) reopens the project's conversation as `claude --resume <session> --remote-control "Jarvis - <project>"` in a minimized console, so it appears in the Claude app with its history; Jarvis closes that window (`_close_remote`) before its next run in the project so the two never write to the same conversation. Interactive sessions stop at Claude Code's folder-trust prompt, so an untrusted folder (`trusted()` reads `~/.claude.json`) opens visibly instead. When a run finishes while you're away (`CODE_NOTIFY`), `_notify_finish` sends an ntfy notification with **Continue on phone** and, if something was refused, **Allow** buttons (`do: phone` / `do: approve` in `notify.handle_action`).
- **Billing** — `CODE_USE_API_KEY = False` strips `ANTHROPIC_API_KEY` from Claude Code's environment so it uses the user's Claude login (the `.env` key would otherwise take precedence).

## Delegated tasks and MCP servers

Three tiers, cheapest first: **direct API calls** in Python where the data is fixed (Wolfram, YouTube captions — one Haiku call at most); **Haiku dispatch** picks the action; **Claude Code** (`claude -p`, the user's subscription) does anything open-ended with MCP tools. MCP tool schemas never go into `brain.ask_claude`.

- **`delegate` action** (`core/delegate.py`) — `delegate.spec(service)` → `coding.start(service=spec)`: runs in `jarvis_delegate/<service>/` with `--mcp-config jarvis_memory/mcp/<service>.json --strict-mcp-config` (only that service's servers; the file has `${VAR}` references, no secrets — Claude Code expands them from the environment). `connectors: True` drops `--strict-mcp-config` so claude.ai connectors and user-scope servers load too (`google`, `general`, and `jobs` with `JOBS_INDEED_CONNECTOR` — Indeed's MCP is claude.ai-connector-only).
- **Google** — the claude.ai connectors (connected at claude.ai → Settings → Connectors). Their tool names can't be known in advance, so `scripts/setup_mcp.py google` asks Claude Code for them once and `delegate.classify` splits them by name: send/delete/update/share/label… → asked about, the rest (read, search, draft, create) → allowed. Saved in `jarvis_memory/mcp/google_tools.json` (editable); `delegate.google_ready()` needs it.
- **Callbacks** — `coding.start(..., quiet=True, on_done=fn)` lets a caller speak the result itself (the briefing's inbox check).
- **Allowed vs. asked** — each service's `allowed` tools run freely (reading, drafts, creating new files); sending, editing calendars, deleting and sharing are refused and come back as denials → "allow it" (`coding.approve` → `delegate.run(..., extra_allowed)`).
- **State** — a service is a `coding._state["projects"]` entry `jarvis-<service>` with `"service"` set: never the current project, hidden from project lists, session resumed for follow-ups until idle `DELEGATE_SESSION_HOURS`. Same Stage `code` panel (titled "Claude — <label>"), phone notification and Remote Control handoff (with the MCP flags) as coding runs.
- **Results** — long answers are written to `result.md` (deleted before each run) and shown as a Stage note; only the short summary is spoken.
- **Defaults** — `DELEGATE_MODEL = "sonnet"`, `DELEGATE_EFFORT = "low"`.
- **Coding servers** — `scripts/setup_mcp.py coding` registers Context7, Playwright, Postman (`/minimal` endpoint), Firebase, Docker MCP Gateway and Postgres (`postgres-mcp --access-mode=restricted`, read-only) at user scope. `mcp__context7`, `mcp__playwright`, `mcp__postgres` are in `CODE_ALLOWED_TOOLS`; Docker/Firebase/Postman tools are approved by voice.
- **Resume** — `jarvis_memory/resume.md` / `.txt`, or `resume.pdf` turned into text once (`resume.pdf.txt`, redone when the PDF changes; text costs Claude Code far fewer tokens than a PDF, which it also reads as page images) — `delegate.resume_file()`, read only by `jobs` tasks (not in `memory.md`, which goes into every prompt).

## Memory and agenda

Everything lives in `jarvis_memory/` (git-ignored):

- **`memory.md`** — facts about the user, one bullet each under `## About me / People / Preferences / Places / Working on / Other` ("Working on" = what they're practicing or aiming for, mostly from check-in answers; Jarvis brings it up when relevant). The user may edit it freely (on the Stage: "what do you remember about me"); it's re-read when it changes. Every bullet goes into the system prompt as a second cached block, numbered so the `memory` action can forget/update by number. Bullets ending in `(auto)` came from the background review, which may only update/delete `(auto)` facts.
- **`conversations/YYYY-MM-DD.jsonl`** — everything said (`state.on_log` → `memory.log`). After `MEMORY_REVIEW_IDLE_MIN` quiet minutes, `memory.review()` asks Haiku for add/update/delete ops and `moment` ops (events to check in about, → `checkins.add_moment`). Never secrets, agenda items, misheard nonsense, or temporary things phrased as facts ("this weekend"): an upcoming event is a moment, and only what stays true after it is a fact. `daily/YYYY-MM-DD.md` summaries of past days are written hourly (no "unfinished items": the agenda tracks what's due); the last 3 go in the prompt. `memory recall` answers "what did we talk about…" from them.
- **`chat_history.json`** — `CHAT_HISTORY`, restored at startup if under `CHAT_HISTORY_KEEP_MIN` old.
- **`agenda.json`** — items `{id, kind: deadline|appointment|task|timer, text, group, when | repeat, remind, done, skip}`. A recurring item is one rule (`repeat: {every, days, time, from, until, interval}`); occurrences are keyed by date, so done/skip apply per week. Occurrences due before the item was created don't count. Nothing is ever called overdue: past items simply aren't mentioned (prompt, spoken list, Stage, briefing) — Jarvis only talks about what's due today or coming up; "I submitted it" late still marks the right occurrence. Python does all date math; the prompt gets `NOW`, every item's id and schedule and the next 7 days. Reminder specs: `"30m"`, `"2h"`, `"1d@19:00"` (defaults per kind in `config.AGENDA_REMIND_DEFAULTS`); fired reminders are recorded, so restarts neither repeat nor (within 15 min) miss them. Reminders wait until Jarvis is idle. The `timer` action is stored here too. Recurring adds are read back for a spoken yes/no; "this semester" uses the semester end from memory or asks for it.

## Check-ins and personality

`core/checkins.py`, data in `jarvis_memory/checkins.json`. The aim: Jarvis feels like a friend who remembers, not only an assistant.

- **Moments** — events a friend would ask "how did it go?" about (`checkins.WORTH_ASKING`: exams, interviews, appointments, presentations, trips, being sick…; never routine coursework, bills, chores or anything recurring). Dated one-off agenda items are classified once by Haiku (`classify_agenda`, background thread); the memory review adds ones mentioned in conversation (`"op":"moment"` → `add_moment`, deduped by date + similar words). Agenda-linked moments follow their item (moved, deleted, skipped).
- **When** — from 2 h after a timed event (18:00 for an untimed appointment, next morning for untimed deadlines / `after: next_day`) until `CHECKIN_EXPIRE_DAYS`. Asked once (once more after 6 h if unanswered; "not now" = never), max `CHECKIN_MAX_PER_DAY`, 2 h apart, and only at a natural moment: `before_turn` (first command after `CHECKIN_GAP_MIN` quiet, or small talk) → Claude's live prompt says CHECK-IN NEXT → `shape_reply` drops a trailing question from the chat reply → `after_turn` asks ("By the way, how did the quiz go yesterday?") if the turn was light (`_LIGHT` actions or chat), not from the phone, not a follow-up or a long answer.
- **Answer** — the normal answer-listening path; `answered()` records it and the live prompt carries CHECK-IN so Claude returns a `checkin` action: a friendly reply (may offer one concrete help as a question), the outcome, and `remember` facts → memory "Working on" (auto). Recent outcomes and big events in the next 2 days are in the live prompt (luck on "good night").
- **Personality** — `brain.build_system_prompt` opens with PERSONALITY (warm, dry wit; small talk answered like a person; react to life news before helping; no questions to offer help). Also: varied wake acknowledgements (`jarvis._ACKS`), a time-of-day greeting at startup, "thanks" answered locally (`router._THANKS`), and the briefing wishes luck on a big day.

## Voice ID and voice training

Everything lives in `jarvis_memory/voice/` (git-ignored). Model: `models/speaker/nemo_en_titanet_small.onnx` (git-ignored, `setup.sh` downloads it). The WeSpeaker models sherpa-onnx offers separated real speakers poorly in testing (different speakers up to 0.89 cosine); TitaNet-small scored same speaker ≈0.6-0.8 vs. others ≈0-0.3.

- **Enrollment** — "Jarvis, learn my voice": ~30 lines (`voice.ENROLL_LINES` + "open <workspace>") read from a Stage note; each good recording becomes a voiceprint for the current mic and a labeled sample. Repeat per microphone. `profile.npz` holds enrolled + adapted voiceprints (recordings ≥ `VOICE_ADAPT` are added, max 40).
- **Scoring** — mean cosine to the 5 closest voiceprints, over speech frames only (webrtcvad). Under 1 s of speech both thresholds drop by 0.08. Verdicts: `owner` / `unsure` / `other`, `new_mic` (no enrollment on this mic → allowed with a one-time hint), `none` (off / not enrolled / too short).
- **Modes** (`VOICE_ID_MODE`, overridden by "turn on/off voice lock" → `settings.json`) — `shadow` logs scores only; `enforce`: unsure → "say it again?"; other after the wake word → ignored silently (also before "Yes sir" if "Jarvis" itself clearly wasn't you); other when Jarvis was made to listen (orb click, after "Yes sir", answering its question) → "Who is this?", logged to `visitors.jsonl`, refused. `agent.confirm` ignores a "yes" that clearly wasn't you. Phone commands aren't checked (Tailscale-authenticated). `VOICE_GUEST_MODE` lets strangers chat but turns their actions into a refusal.
- **Data** — every screened command is saved (`samples/<day>/<id>.wav` + `samples.jsonl`), except refused strangers in enforce mode. Labels come from enrollment, "no, I said …", the dashboard ✎/✓, or `scripts/voice/label.py`. Unlabeled recordings are pruned after `VOICE_SAMPLE_DAYS`. One-breath commands store a `prefix` ("Jarvis, ") so a label of the command alone still matches the audio.
- **Fixes and vocabulary** — a correction diffs heard vs. meant words; a pair seen `VOICE_RULE_MIN` times is applied to every transcript (`corrections.json`). Corrected words and `vocabulary.txt` go into the Whisper prompt. "Open <misheard app>" also fuzzy-matches app/workspace names (`files.closest_name`). Comparisons normalize number words ("three" = "3").
- **Scripts** (`scripts/voice/`) — `benchmark.py` (WER + speed per model on your labeled recordings, same decoding as Jarvis), `label.py` (play + label recordings in the terminal), `calibrate.py` (threshold suggestions from your scores), `export.py` (zip for fine-tuning), `finetune.py` (LoRA on `openai/whisper-small.en`, GPU/Colab only → CTranslate2 folder for `WHISPER_MODEL`).

## Workspaces

Workspaces are defined in `workspaces.json`. Each workspace is a named object with an optional `description` and an `items` array. Each item has a `type` (`url`, `vscode`, `file`, or `app`) and a `path`.

Edit them at `http://localhost:5151/workspaces`. Changes save instantly and hot-reload the running assistant's voice triggers.

## Configuration (`core/config.py`)

| Variable | Purpose |
|---|---|
| `PIPER_VOICE` / `PIPER_SPEED` / `PIPER_SENTENCE_PAUSE` / `PIPER_DASH_PAUSE` | Natural offline voice: a folder in `models/tts/` (`vits-piper-*-medium` from sherpa-onnx's `tts-models` release; "high" voices are ~5x slower). Empty = SAPI |
| `VOICE_NAME` | SAPI voice name fragment, e.g. `"Zira"` (empty = system default) |
| `SPEECH_RATE` | TTS rate, -10 to 10 |
| `PRONUNCIATIONS` | Words Jarvis says wrong → SAPI phonemes (e.g. `"Danil": "d ax n iy 1 l"`); "pronounce X as Y" adds one by voice |
| `WAKE_WORD` | Defaults to `"jarvis"` |
| `WAKE_GRAMMAR` / `WAKE_DECOYS` | Vosk listens only for the wake word plus sound-alike decoy words (default). Full-vocabulary mode (`False`) falls behind real time in loud rooms; it's capped to 4 s utterances and a 1 s backlog guard skips ahead |
| `SPEECH_OVER_NOISE` | How much louder than the room (median of the last 3 s) speech must be, over a 0.4 s window, for end-of-speech detection. Raise if recordings run on in noise, lower if quiet speech gets cut off |
| `WHISPER_MODEL` | faster-whisper model (`base.en`) or a fine-tuned model folder. Transcription runs with no temperature fallback (noisy audio otherwise took 20-40 s) and strips echoes of the hint prompt (`asr.strip_prompt_echo`). Pick with `scripts/voice/benchmark.py` |
| `WHISPER_BEAM` / `WHISPER_VOCAB_PROMPT` | Beam size (1 = greedy); whether your vocabulary goes into the hint prompt |
| `VOICE_ID_MODE` | `"off"` / `"shadow"` (score only) / `"enforce"` (only your voice) — voice "turn on/off voice lock" overrides it |
| `VOICE_ACCEPT` / `VOICE_REJECT` / `VOICE_ADAPT` | Score thresholds (0.45 / 0.25 / 0.60); tune with `scripts/voice/calibrate.py` |
| `OWNER_NAME` / `VOICE_GUEST_MODE` | Your name in refusals (empty = from memory); let strangers chat (no actions) |
| `VOICE_SAVE_SAMPLES` / `VOICE_SAMPLE_DAYS` / `VOICE_RULE_MIN` | Save command recordings; days to keep unlabeled ones; corrections before a fix is automatic |
| `INPUT_DEVICE` | Mic name fragment to pin (e.g. `"AirPods"`); empty = follow the Windows default, switching automatically when it changes |
| `FOLLOWUP_WINDOW` | Seconds to wait after the wake word for a one-breath command before saying "Yes sir" (0.9 — a natural pause after "Jarvis," is ~0.3 s) |
| `MPV_EXE` / `ESRGAN_EXE` | Paths to optional external tools |
| `USE_DESKTOP_APP` | Open the dashboard in the Electron app (`desktop/`) instead of a browser tab |
| `CESIUM_ION_TOKEN` (`.env`) | Optional: 3-D terrain and holographic buildings on the Stage globe |
| `CODE_PERMISSION_MODE` / `CODE_ALLOWED_TOOLS` | What background Claude Code may do without asking |
| `CODE_MODEL` / `CODE_EFFORT` | Default Claude Code model and thinking effort (voice overrides per request) |
| `PROMPT_CACHE_TTL` | Prompt cache lifetime: `"5m"` (cheaper writes, for back-to-back commands) or `"1h"` |
| `DORMANT_AFTER_MIN` | On battery: minutes with no input/command before the mic closes (locked = right away); 0 = never |
| `PHONE_PUSH` / `NTFY_SERVER` / `NTFY_TOPIC` / `PHONE_AWAY_MIN` / `PHONE_URGENT_HOURS` | Phone notifications (ntfy): on/off, server, topic (empty = random, saved), how long away before reminders go to the phone, deadlines that always go |
| `CODE_NOTIFY` | Phone notification when a Claude Code run finishes: `"away"` (default), `"always"` or `"never"` |
| `REPO_DIRS` / `GITHUB_USER` / `CODE_MAX_PARALLEL` / `CODE_BRANCH_PER_TASK` | Where your repos live, your GitHub user (for cloning), parallel Claude Code runs, branch per task |
| `ANSWER_WINDOW` | Seconds Jarvis waits for you to start answering its question before going back to the wake word |
| `HOME_CITY` / `UNITS` / `BRIEFING_NEWS` | Daily breakdown: weather city (empty = from memory, or ask once), °F/°C, news searches |
| `MEMORY_REVIEW_IDLE_MIN` / `CHAT_HISTORY_KEEP_MIN` | Quiet minutes before the memory review runs; how recent a conversation must be to survive a restart |
| `AGENDA_REMIND_DEFAULTS` | Default reminders per agenda kind |
| `CHECKINS` / `CHECKIN_MAX_PER_DAY` / `CHECKIN_GAP_MIN` / `CHECKIN_EXPIRE_DAYS` | Check-ins on/off, per-day cap, quiet minutes that make the next command a natural moment to ask, days after an event it's still worth asking |
| `CODE_USE_API_KEY` | `False` = Claude Code uses your Claude login, `True` = bills the `.env` API key |
| `DELEGATE_MODEL` / `DELEGATE_EFFORT` / `DELEGATE_SESSION_HOURS` | Claude Code model/effort for delegated tasks; idle hours before a service's conversation starts fresh |
| `JOBS_INDEED_CONNECTOR` / `RESUME_FILE` | Also use the claude.ai Indeed connector for job searches; your resume, without extension (ranks results by fit) |
| `BRIEFING_EMAIL` | Unread mail in the daily breakdown (needs the Google connectors) |

## Shutting down

"Good night" / "shut down" (router, or the `shutdown` action for other phrasings) → if Claude Code is mid-run, Jarvis asks first (and stops the runs); says good night with good luck for tomorrow's big events (`checkins.tomorrow`), sets `state.shutdown`; the main loop then stops music, saves the Stage, closes the desktop app and exits (Ctrl+C takes the same path). "Shut down my computer" is `agent`.

## Background mode

**Installed (2026-10-11)** as the `JARVIS` login task. `python jarvis.py --background` skips opening the browser, sends all output to `logs/jarvis.log` (rotated), hides the console windows of programs it runs, and refreshes the file/bookmark/app indexes hourly. `scripts/install_startup.ps1` registers that command (via `pythonw.exe`, no console) as a login task; `scripts/uninstall_startup.ps1` removes it.

Only one Jarvis runs at a time: `jarvis.py` exits if `PORT` is taken. To run a dev copy in a console, stop the task first (`Stop-ScheduledTask -TaskName JARVIS`, then `Start-ScheduledTask` afterwards).

## Claude API usage

The project uses `claude-haiku-5-5` (`config.MODEL`) for all Claude calls:
- `brain.ask_claude` — main command dispatch; returns a single JSON object; `max_tokens=1000` (1500 for in-depth answers), `effort: "low"`
- `agent.run_agent` — tool-use loop for tasks no action covers; up to 8 steps, `max_tokens=3000` per step, `effort: "low"`
- everything else (summaries, image analysis, extraction, one-liners) — thinking off

**Thinking** — Haiku 5.5 thinks before answering unless told not to (default effort `medium`), and thinking tokens count toward `max_tokens`. Pass `**config.THINK_LOW` (dispatch, agent) or `**config.NO_THINK` (one-shot text) on every call, and read replies with `config.reply_text(resp)` — with thinking on, `content[0]` is a thinking block, not the answer. Thinking can't be given a token budget on this model (`budget_tokens` is rejected); `effort` is the control. Switching from Haiku 4.5 was benchmarked on the real dispatch prompt (22 commands): same accuracy and speed at `effort: "low"`, ~8x cheaper per command, ~30% more input tokens (newer tokenizer).

History keeps the last 12 messages, always starting on a user turn. Side results (image analysis, web search, input-folder contents, agent results) are added with `brain.remember()`.

**Prompt caching** — `brain.ask_claude` sends the system prompt as up to three blocks: the fixed action list and the memory block (both marked `cache_control`), then the parts that change per call (time, agenda, Stage panels, coding project, answer length). The agent loop moves one marker to its newest message each step. Haiku 5.5 caches prompts of 512+ tokens (`config.CACHE_MIN_TOKENS`), so the action list is always cached. Each call logs `[brain] N tokens in (… from cache / written to cache / not cached: under the minimum)`. Keep anything that varies out of `build_system_prompt` — one changed byte there re-writes the cache. `PROMPT_CACHE_TTL` (`config.py`): `"5m"` or `"1h"`.

**Answer length** — `core/verbosity.py`: short by default (main point, ~20 words); phrases like "explain in depth" / "go into more detail" switch the current command to a full answer. Every prompt that produces speech uses `verbosity.rule()`.
