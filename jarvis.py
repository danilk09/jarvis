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
import re
import threading
import time

from core import actions, agenda, audio, brain, coding, desktop, files, memory, music, notify, router, server, stage, state, verbosity
from core import speech_data, voice, voice_id
from core import tts
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

    # I/O folders. Processed input files go to a per-session archive folder, created only
    # when the first file is archived (media.archive_input_file).
    os.makedirs(config.JARVIS_INPUT_DIR, exist_ok=True)
    os.makedirs(config.JARVIS_OUTPUT_DIR, exist_ok=True)
    state.session_archive = os.path.join(config.INPUT_ARCHIVE_DIR, f"session_{time.strftime('%Y%m%d_%H%M%S')}")

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
    if brain.restore_history():
        print("  Picked up the conversation from the last run")
    memory.start()     # conversation log + background review of what's worth remembering
    phone_topic = notify.start()   # ntfy topic for reminders on the phone
    agenda.start()     # reminders and timers
    voice.start()      # voice ID model + profile, prune old recordings
    audio.on_wake.extend([stop_tts, music.duck])   # interrupt speech and duck music instantly
    tts.on_speech_start.append(lambda: music.duck("speech"))   # anything Jarvis says, even unprompted
    tts.on_speech_end.append(lambda: music.unduck("speech"))
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
    if config.PHONE_PUSH:
        print(f"  Phone alerts: ntfy app -> subscribe to \"{phone_topic}\" on {config.NTFY_SERVER}")
    print(f"  Workspaces: {list(state.workspaces.keys()) or 'none'}")
    print(f"  AI: Claude Haiku 4.5   OS: {config.OS}")
    print(f"  Input folder:  {config.JARVIS_INPUT_DIR}")
    print(f"  Output folder: {config.JARVIS_OUTPUT_DIR}")
    print(f"{'='*50}")
    print("\n  Say 'Jarvis' to activate — or all at once: 'Jarvis, open Discord'.\n")


# An answer that means "never mind" ends the exchange instead of going to Claude
_END_ANSWERS = {"never mind", "nevermind", "forget it", "cancel", "nothing", "no thanks", "no thank you",
                "that's all", "that's it", "not now", "skip it"}
_MAX_FOLLOWUPS = 3   # questions answered in a row without the wake word
_RETRY = "I didn't catch that. Say it again?"


def _open_question(since):
    """The question Jarvis asked after `since` that nothing has listened for yet, or ""."""
    q = tts.last_question
    if q["time"] > since and q["time"] > audio.last_listen[0] and not suppressed.is_set():
        return q["text"]
    return ""


def _listen_for_answer(question):
    """Jarvis asked something: listen for the answer without the wake word. Returns the
    next command (the answer, with the question for context) or None."""
    audio.disable_wake()
    music.duck()
    state.push_state("activated")
    print(f'  Listening for an answer to: "{question}"')
    answer = audio.listen_for_command(onset_timeout=config.ANSWER_WINDOW)
    answer = re.sub(r"^\W*(hey |ok |okay )?jarvis\b\W*", "", answer or "", flags=re.IGNORECASE).strip()
    if not answer or answer.lower().rstrip(".!?") in _END_ANSWERS:
        return None
    answer = voice.screen(answer, "answer")      # someone else answering → "Who is this?"
    if not answer:
        return None
    if question == _RETRY:       # a second try at the same command, not an answer
        return answer
    return f'(Answering your question "{question}") {answer}'


def _next_command():
    """Block until the wake word or a phone command. Returns (command, from_phone)."""
    audio.enable_wake()
    waiting_since = time.time()
    while not audio.activated.wait(timeout=0.1):
        try:
            return server.phone_commands.get_nowait(), True
        except queue.Empty:
            pass
        # Something running in the background (Claude Code, a reminder) asked a question
        if not tts.busy() and _open_question(waiting_since):
            answer = _listen_for_answer(_open_question(waiting_since))
            if answer:
                return answer, False
            music.unduck()
            state.push_state("idle")
            audio.enable_wake()
            waiting_since = time.time()
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
        return voice.screen(command, "wake"), False   # not your voice → ignored
    if voice.wake_is_stranger():
        return None, False

    speak("Yes sir.")
    wait_tts()
    state.push_state("activated")
    command = audio.listen_for_command()
    if not command:
        return command, False
    return voice.screen(command, "dashboard" if audio.wake_source() == "dashboard" else "prompted"), False


def _finish(since=None, followups=0):
    """Once Jarvis has finished speaking: if it asked a question, listen for the answer and
    return it as the next command; otherwise go back to idle and return None."""
    wait_tts()
    question = _open_question(since) if since and followups < _MAX_FOLLOWUPS else ""
    if question and not audio.activated.is_set():
        answer = _listen_for_answer(question)
        if answer:
            return answer
    if not audio.activated.is_set():   # re-activated mid-response: keep music ducked
        music.unduck()
    state.push_state("idle")
    return None


def main():
    _startup()
    followup, followups = None, 0
    while True:
        try:
            if followup:                 # the answer to a question Jarvis just asked
                command, from_phone, followup = followup, False, None
                followups += 1
            else:
                print("Waiting for wake-word or phone command...")
                command, from_phone = _next_command()
                followups = 1 if command and command.startswith("(Answering your question") else 0
            audio.disable_wake()
            turn_start = time.time()
            if command is None:
                music.unduck()
                state.push_state("idle")
                continue
            if from_phone:
                print(f'\n  Phone command: "{command}"')
                voice_id.current.clear()
                voice_id.current.update(verdict="none", score=None, sample="", guest=False)
            elif not command:
                print("  No command heard.\n")
                speak(_RETRY)
                followup = _finish(turn_start, followups)
                continue

            # "No, I said open Discord": label the last recording, learn the fix, run the right command
            fixed = None if from_phone else speech_data.parse_correction(command)
            if fixed:
                sid = voice_id.current.get("sample")
                newest = speech_data.recent_sample()
                prev = speech_data.recent_sample(skip=1 if newest and newest[0] == sid else 0)
                if prev and speech_data.label(prev[0], fixed, "voice"):
                    state.mark_log(prev[0], text=fixed, corrected=True)
                    print(f'  Correction: "{prev[1]}" -> "{fixed}"')
                    command = fixed

            # the dashboard and the conversation log show just what was said, not the question
            state.push_state("thinking", transcript=re.sub(r'^\(Answering your question ".*?"\) ', "", command),
                             sample="" if from_phone or fixed else voice_id.current.get("sample", ""))
            if verbosity.update(command):
                print("  Detailed answer requested.")
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
            if voice_id.current.get("guest") and (response or {}).get("mode") == "action":
                response = voice.guest_reply()   # strangers may chat, not act

            audio.enable_wake()   # let the user interrupt Jarvis mid-response
            try:
                actions.handle_response(response)
            except Exception as e:
                print(f"  Error in handle_response: {e}")
            followup = _finish(turn_start, followups)

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
