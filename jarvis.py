#!/usr/bin/env python3
"""
JARVIS - Voice-Activated AI Assistant
Say "Jarvis" -> speak your command -> Claude figures out what to do.

    python jarvis.py                # normal (development) mode
    python jarvis.py --background   # 24/7 mode: no browser, logs to logs/jarvis.log

The modules live in core/ — see CLAUDE.md for the layout.
"""

from core import config

if config.BACKGROUND_MODE:
    from core import background
    background.setup_logging()   # before anything prints (pythonw.exe has no console)

import os
import queue
import threading
import time

from core import actions, audio, brain, coding, desktop, files, music, router, server, stage, state
from core.tts import speak, stop_tts, suppressed, wait_tts

# Dismissals and filler that never need the AI
BYPASS_COMMANDS = {
    "never mind", "nevermind", "forget it", "forget that", "cancel", "stop",
    "nothing", "nope", "no", "abort", "disregard", "ignore that",
    "never mind that", "scratch that", "skip it",
    "um", "uh", "hmm", "hm", "okay", "ok", "yeah", "yes", "alright",
    "thanks", "thank you", "cool", "got it",
}


def should_bypass_ai(text):
    return text.strip().lower().rstrip(".,!?") in BYPASS_COMMANDS


def _startup():
    state.workspaces.update(files.load_workspaces())

    # I/O folders, plus a per-session archive for processed input files
    os.makedirs(config.JARVIS_INPUT_DIR, exist_ok=True)
    os.makedirs(config.JARVIS_OUTPUT_DIR, exist_ok=True)
    state.session_archive = os.path.join(config.INPUT_ARCHIVE_DIR, f"session_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(state.session_archive, exist_ok=True)

    # Tailscale: HTTPS cert so the phone page can use the mic
    ts_ip, ts_fqdn = server.detect_tailscale()
    phone_url = ""
    if ts_fqdn:
        phone_url = (f"https://{ts_fqdn}:{config.PORT}/phone" if server.setup_tailscale_https(ts_fqdn)
                     else f"http://{ts_ip}:{config.PORT}/phone")
    elif ts_ip:
        phone_url = f"http://{ts_ip}:{config.PORT}/phone"
    dash_url = (f"https://{ts_fqdn}:{config.PORT}" if server.tls["cert"]
                else f"http://localhost:{config.PORT}")

    state.dash_url = dash_url
    restored = stage.load(config.STAGE_STATE_FILE)   # pick up where the last run left off
    if restored:
        print(f"  Stage: restored {restored} panel{'s' if restored != 1 else ''} from last time")
    coding.init()                                     # Claude Code projects + preferences file
    stage.open_dashboard = lambda: desktop.open_dashboard(dash_url)   # Stage content with no window open
    server.start()
    if not config.BACKGROUND_MODE:
        time.sleep(0.6)   # give Flask a moment to bind before opening the window
        desktop.open_dashboard(dash_url)

    # Indexes build in the background so startup isn't slow
    for build in (files.build_file_index, files.build_bookmark_index, files.build_app_index):
        threading.Thread(target=build, daemon=True).start()
    if config.BACKGROUND_MODE:
        background.start_index_refresh()

    brain.init_chat_history(state.workspaces)
    audio.on_wake.extend([stop_tts, music.duck])   # interrupt speech and duck music instantly
    audio.start()
    speak("Ready for your command.")

    print(f"\n{'='*50}")
    print("  JARVIS is ready" + ("  (background mode)" if config.BACKGROUND_MODE else ""))
    print(f"  Dashboard:  {dash_url}")
    if phone_url:
        tls_note = "" if server.tls["cert"] else " (no HTTPS — mic may be blocked; run 'tailscale cert' manually)"
        print(f"  Phone mic:  {phone_url}{tls_note}")
    else:
        print("  Phone mic:  Tailscale not detected — install Tailscale for remote phone access")
    print(f"  Workspaces: {list(state.workspaces.keys()) or 'none'}")
    print(f"  AI: Claude Haiku 4.5   OS: {config.OS}")
    print(f"  Input folder:  {config.JARVIS_INPUT_DIR}")
    print(f"  Output folder: {config.JARVIS_OUTPUT_DIR}")
    print(f"{'='*50}")
    print("\n  Say 'Jarvis' to activate — or all at once: 'Jarvis, open Discord'.\n")


def _next_command():
    """Block until the wake word or a phone command. Returns (command, from_phone)."""
    audio.enable_wake()
    while not audio.activated.wait(timeout=0.1):
        try:
            return server.phone_commands.get_nowait(), True
        except queue.Empty:
            pass
    audio.disable_wake()
    audio.activated.clear()
    suppressed.clear()
    state.push_state("activated")
    print("\n  Activated!")

    # "Jarvis, open Discord" in one breath — skip the "Yes sir" round trip
    command = audio.capture_followup()
    if command is None:
        return None, False   # false wake from background speech
    if command:
        return command, False

    speak("Yes sir.")
    wait_tts()
    state.push_state("activated")
    return audio.listen_for_command(), False


def _finish():
    """Return to idle once Jarvis has finished speaking."""
    wait_tts()
    if not audio.activated.is_set():   # re-activated mid-response: keep music ducked
        music.unduck()
    state.push_state("idle")


def main():
    _startup()
    while True:
        try:
            print("Waiting for wake-word or phone command...")
            command, from_phone = _next_command()
            audio.disable_wake()
            if command is None:
                music.unduck()
                state.push_state("idle")
                continue
            if from_phone:
                print(f'\n  Phone command: "{command}"')
            elif not command:
                print("  No command heard. Say 'Jarvis' again.\n")
                speak("I didn't catch that. Try again.")
                _finish()
                continue

            state.push_state("thinking", transcript=command)
            if should_bypass_ai(command):
                print("  Bypassed AI (dismissal command).")
                _finish()
                continue

            response = router.route(command)
            if response:
                print("  Handled locally (no AI call).")
            else:
                print("  Thinking...")
                response = brain.ask_claude(command, state.workspaces)

            audio.enable_wake()   # let the user interrupt Jarvis mid-response
            try:
                actions.handle_response(response)
            except Exception as e:
                print(f"  Error in handle_response: {e}")
            _finish()

        except KeyboardInterrupt:
            speak("Shutting down. Goodbye.")
            music.stop()
            stage.flush()
            wait_tts()
            print("\n  JARVIS shutting down. Goodbye.")
            break
        except Exception as e:
            print(f"  Unexpected main loop error: {e}")
            state.push_state("idle")
            time.sleep(0.5)


if __name__ == "__main__":
    main()
