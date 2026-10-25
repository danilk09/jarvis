# Phase 3: the PC as a client of the Pi brain

Mobile Jarvis Phase 3. Phase 2 (the brain on the Pi 5, power page, health checks) is done. This
phase makes the **Pi the only brain** and turns the PC into a **client**: the PC keeps what only it
can do (wake word, mic, speakers, the Stage window, opening things on the PC), and everything you
say, anywhere, goes to the Pi. One memory, agenda and history, so there is nothing to sync or merge.

## Decisions (2026-10-22)

1. **One brain, the Pi.** The PC never runs a second brain. Until this phase ships, PC Jarvis stays
   off while the Pi is the brain.
2. **PC action while the PC is off** ("open Discord" from the phone): Jarvis says the PC is off and
   offers to do it when the PC is back. Yes → queued, run when the PC client connects ("While you
   were out you asked me to open Discord - done").
3. **PC can't reach the brain:** if the Pi answers but Jarvis is off, offer to start him (power page
   API); if the Pi doesn't answer at all, say so. No local fallback brain.
4. **Claude Code runs on the Pi** (any repo, Jarvis or not). When a task finishes, its `jarvis/<task>`
   branch is pushed to that repo's GitHub automatically; `main`, merges and other branches still
   need your OK. New projects get a **private GitHub repo** after a one-time OK per project.
5. **Coding catch-up at the PC:** the Pi keeps a ledger of new repos, new `jarvis/` branches and
   updated repos. "Update my coding sessions" at the PC clones new repos into `Desktop\GitHub`,
   fetches/pulls existing ones (fast-forward only, never touching local changes), and lists the
   branches waiting for review.
6. **Where replies go:** to where the command came from. A phone command is **also spoken at the PC**
   when the PC reports you're active there. Everything shows on the dashboard either way.

## Update 2026-10-23: the phone page first

- **No pocket device.** The `/phone` page is how you talk to Jarvis away from the PC, so Phase 4's
  second Pi is dropped and the protocol below serves the phone first (`source: "phone"`).
- **M1 is built** (`core/turns.py`), phone-first: turns carry a source; `GET /api/client/events`
  streams `line` / `user` / `state` / `listen` events; `GET /api/say/<id>` is a line as Piper
  speech; `POST /api/turn` takes typed commands; `scripts/pi/fake_client.py` is the test client.
  No client key yet: like `/phone` and the dashboard, these are only reachable over Tailscale.
  The key comes with the PC client (M2), which still needs `say`/`do` routing.
- **The phone page** was rebuilt in the React app (`jarvis-dashboard/src/phone/`): replies spoken
  (its own turns and anything Jarvis says between turns), an answer bar when he asks something,
  typed commands, the whole Stage, notifications that open the page. It records with the phone's
  own mic by default, since AirPods transcribed much worse.
- **Claude Code** runs with full permissions (except pushing to main and deleting repos) and
  pushes its `jarvis/` branches itself, which covers most of M6's auto-push.
- **M2 is built** (`core/client.py`, `python jarvis.py --client <pi url>`): wake word, Whisper, voice
  ID and Piper stay on the PC; turns go to the Pi as `source: "pc"`, its lines and questions come
  back and are spoken/answered at the PC; a new wake-word command while the Pi waits for an answer
  is sent as `fresh` and ends that wait. The presence heartbeat is in too (`notify.away()` on the
  Pi). Still no client key (Tailscale is the boundary, as for `/phone`). The PC window still shows
  the PC's own dashboard (orb), not the Pi's: that's M7.
- **M3 is built** (`core/pc.py`): PC actions from any client run on the PC; what they say and ask
  goes back to whoever gave the command; PC off → offer → `pc_queue.json` → run on reconnect.
- **M4 is built** (`turns.targets`): lines and questions carry `to`; phone replies are also
  spoken at the PC while you're there; Jarvis's own lines go to the PC if you're there, else the
  phone. Locked PC = away.
- **M5 is built**: the feed survives a restart (last 40 lines), and a day picker on the dashboard
  (Activity tab) and the phone (🕘) opens any earlier day. Lines now record phone/PC.
- **M6 is built** (`core/repos.py`): one `jarvis` branch per repo (user's choice, 2026-10-24; `jarvis-<task>` only when asked), pushed after each run, one GitHub offer per
  new project (private repo), a ledger, and "update my coding sessions" on the PC.
- **Next:** M7 (launcher + Electron → Pi).

## How it fits together

```
 phone (/phone page) ─┐                          ┌─ PC client (jarvis.py --client)
                      │   HTTPS over Tailscale   │   wake word, mic, Whisper, voice ID
 pocket device ───────┼──►  Pi: the brain  ◄─────┤   Piper on the PC speakers, music (mpv)
                      │   router, Claude,        │   PC actions: apps, workspaces, URLs,
 dashboard / Stage ◄──┘   memory, agenda,        │   files, screenshots, agent, repo sync
 (served by the Pi)       Claude Code, Stage     └─ Electron window = the Pi's dashboard
```

**A turn at the PC:** wake word → record → Whisper and voice ID on the PC → `POST /api/turn`
`{text, source: "pc", voice}` → the Pi routes/thinks as today → each spoken line goes back as a
`say` event → Piper speaks it on the PC (its speech envelope is reported back, so the dashboard orb
moves) → a PC-only action comes back as a `do` event → the PC runs it and posts the result.

**When Jarvis asks a question** (confirmations, check-ins, "which one?"): the Pi sends `listen` to the
turn's source. The PC records the answer; the phone answers with its next message (as today).
Saying the wake word while he talks interrupts: the PC stops its speech and tells the Pi.

## Protocol (Pi ⇄ PC)

| Direction | Call | Purpose |
|---|---|---|
| PC → Pi | `GET /api/client/events` (SSE) | `say {id, text, turn}`, `listen {id, question, window_s}`, `do {id, action}`, `stop`, `ping` |
| PC → Pi | `POST /api/turn` | a command: text (+ voice verdict, optional image), `source: "pc"` |
| PC → Pi | `POST /api/client/result` | outcome of a `do`: `{id, ok, result, data}` (screenshot image upload included) |
| PC → Pi | `POST /api/client/answer` | the answer to a `listen` (or "nothing heard") |
| PC → Pi | `POST /api/client/speech` | the speech envelope / start / end of a line, for the orb |
| PC → Pi | `POST /api/client/presence` | every 15 s: input idle seconds, music state; also the heartbeat |
| PC → Pi | `POST /api/client/index` | on connect / hourly: workspace, app and bookmark names for the prompt |

All calls carry a client key (`jarvis_memory/client.json` on the Pi, copied into the PC's `.env`
as `JARVIS_CLIENT_KEY`). The phone keeps using `/phone-command` and `/status`, unchanged.

## Pi changes

- `server.py`: the client endpoints above. `phone_commands` becomes a general turn queue where each
  turn carries its source (`phone`, `pc`, later `device`).
- `state.py`: the current turn (source, sink), client presence (online, last seen, idle seconds).
- `tts.py` (headless worker): each line goes to the turn's sink, i.e. the PC client as `say`, the
  phone via the log as today. A phone turn is also sent to the PC when presence says you're active
  (decision 6). Proactive lines (reminders, Claude Code finished, check-ins) go to the PC if you're
  there, else to the phone notification (as now).
- `audio.listen_for_command` (headless): waits for the answer from the current turn's source.
- `actions.handle_response`: a PC-only action → `do` to the client and wait for the result (timeout
  ~20 s). If the PC is offline → "Your PC is off, want me to do it when it's back?" → yes queues it in
  `jarvis_memory/pc_queue.json`; the queue runs when the client connects.
- `brain.py` / `registry.py`: PC actions are always in the prompt again (keeps the cached prompt
  stable); workspace/app/bookmark names come from the client's last index upload
  (`jarvis_memory/pc_index.json`), not the PC's disk.
- `notify.away()`: from client presence.
- History (decision: "see it later on the dashboard"): the activity log is restored from
  `conversations/*.jsonl` at startup, and the dashboard gets a day-by-day history view.
- `coding.py`: auto-push `jarvis/` branches when a task finishes (only `git push origin jarvis/*` is
  allowed); new project → ask once → private GitHub repo (GitHub API with `GITHUB_TOKEN`, which then
  needs repository-creation permission); `jarvis_memory/coding_ledger.json` records every new repo,
  branch and push for the catch-up.

## PC changes

- `jarvis.py --client` (or `CLIENT_OF = "https://raspberrypi.<tailnet>.ts.net:5151"` in config):
  starts audio (wake word, mic), tts (Piper, PC speakers), music, the file/app/bookmark indexes,
  media, voice ID and the desktop window; **not** the router, brain, memory, agenda, check-ins or
  Claude Code. Its loop: wake → capture → screen → `POST /api/turn`, and handles events.
- PC-only handlers run here: workspace, url, app, search, vscode, find_files/file, bookmark,
  screenshot (image uploaded to the Pi's Stage), music, agent (the whole tool loop with its spoken
  confirmations, on the PC), and the new `repo_sync` (decision 5).
- Local fast path for "stop" / music controls, so they react instantly; reported to the Pi after.
- `desktop/main.js` + `desktop.py`: the Electron window loads the Pi's dashboard URL.
- Launcher / shortcut: starts the client. If the Pi's Jarvis is off → "Start Jarvis on the Pi?" →
  power page API; if the Pi is unreachable → says so (decision 3).
- Voice ID, voice recordings and enrollment stay on the PC (the mic is there).

## Milestones

Each is testable before the next starts.

| # | What | Done when |
|---|---|---|
| M1 ✓ | Pi: turn queue with sources, client endpoints, SSE events (key and presence move to M2) | `scripts/pi/fake_client.py` (text in, events printed) holds a conversation with the Pi, including a `listen` |
| M2 ✓ | PC client voice loop (+ presence heartbeat) | "Jarvis, what time is it?" at the PC is answered by the Pi and spoken on the PC; the orb moves; answers to questions and interrupting work |
| M3 ✓ | PC actions + offline queue | "Open Discord" from the PC and from the phone (PC on) opens it; with the PC off it offers, queues, and runs it on reconnect; screenshot lands on the Stage |
| M4 ✓ | Reply routing + proactive messages | phone command spoken at the PC when you're active; reminders/check-ins at the PC or the phone depending on presence |
| M5 ✓ | History | restart the Pi, yesterday's conversation is still on the dashboard; a day picker |
| M6 ✓ | Coding on the Pi | task branch auto-pushed; new project → private repo after OK; "update my coding sessions" clones/pulls on the PC |
| M7 | Launcher + Electron → Pi, power start, docs | the Desktop shortcut gives the full experience; `push_to_pi.ps1`/`pull_from_pi.ps1` kept only as backups |

## Open details (decide during the milestones)

- Presence threshold for "you're at the PC" (input in the last 5 min?) and whether a locked PC
  counts as away (probably yes).
- Should a phone command spoken at the PC wait until the PC is idle, or cut into music? (Probably
  duck music like now.)
- Files that only exist on the PC (`show_file`, "find my resume"): upload to the Pi for the Stage, or
  open them on the PC only.
- The pocket device (Phase 4) speaks the same protocol with `source: "device"`, so this API is
  built with it in mind.
