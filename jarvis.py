#!/usr/bin/env python3
"""
JARVIS - Voice-Activated AI Assistant
Say "Jarvis" -> speak your command -> Claude figures out what to do.

    python jarvis.py                # normal (development) mode
    python jarvis.py --background   # 24/7 mode: no browser, logs to logs/jarvis.log
    python jarvis.py --headless     # the brain without mic/speakers/window (default on Linux: the Pi)

The modules live in core/ — see CLAUDE.md for the layout.
"""

from core import config

if config.BACKGROUND_MODE:
    from core import background
    background.setup_logging()   # before anything prints (pythonw.exe has no console)
    background.hide_child_consoles()

import socket
import sys

# One Jarvis at a time: the login task and a dev run would fight over the mic and the port
_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _probe.bind(("0.0.0.0", config.PORT))
except OSError:
    print(f"Jarvis is already running (port {config.PORT} is in use). If it's the background "
          "task, stop it first:  " + ("Stop-ScheduledTask -TaskName JARVIS" if config.WINDOWS
                                      else "sudo systemctl stop jarvis"))
    sys.exit(1)
finally:
    _probe.close()

import os
import queue
import random
import re
import signal
import threading
import time

from core import actions, agenda, audio, brain, checkins, coding, desktop, files, memory, music, notify, power, router, server, stage, state, verbosity
from core import speech_data, voice, voice_id
from core import tts
from core.tts import speak, stop_tts, suppressed, wait_tts

# Dismissals and filler that never need the AI
BYPASS_COMMANDS = {
    "never mind", "nevermind", "forget it", "forget that", "cancel", "stop",
    "nothing", "nope", "no", "abort", "disregard", "ignore that",
    "never mind that", "scratch that", "skip it",
    "um", "uh", "hmm", "hm", "okay", "ok", "yeah", "yes", "alright",
    "cool", "got it",
}

# After the wake word: a little variety instead of "Yes sir." every time (none end in "?",
# which would count as a question waiting for an answer)
_ACKS = ["Yes sir.", "Yes sir.", "Sir.", "Go ahead, sir.", "Listening."]


def should_bypass_ai(text):
    return text.strip().lower().rstrip(".,!?") in BYPASS_COMMANDS


def _greeting():
    hour = time.localtime().tm_hour
    if hour < 5:
        return "Burning the midnight oil, sir? I'm here."
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    return f"Good {part}, sir."


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
    if not config.HEADLESS:   # Stage content with no window open (the Pi has no screen: it waits)
        stage.open_dashboard = lambda: desktop.open_dashboard(dash_url)
    server.start()
    desktop.on_closed.append(_window_closed)
    if config.OPEN_WINDOW:
        time.sleep(0.6)   # give Flask a moment to bind before opening the window
        desktop.open_dashboard(dash_url)

    # Indexes build in the background so startup isn't slow (the PC's files and apps only)
    for build in () if config.HEADLESS else (files.build_file_index, files.build_bookmark_index,
                                             files.build_app_index):
        threading.Thread(target=build, daemon=True).start()
    if config.BACKGROUND_MODE:
        background.start_index_refresh()

    brain.init_chat_history(state.workspaces)
    if brain.restore_history():
        print("  Picked up the conversation from the last run")
    memory.start()     # conversation log + background review of what's worth remembering
    phone_topic = notify.start()   # ntfy topic for reminders on the phone
    agenda.start()     # reminders and timers
    checkins.start()   # "How did the quiz go?" — asking how important things went
    voice.start()      # voice ID model + profile, prune old recordings
    if not config.HEADLESS:
        audio.on_wake.extend([stop_tts, music.duck])   # interrupt speech and duck music instantly
        tts.on_speech_start.append(lambda: music.duck("speech"))   # anything Jarvis says, even unprompted
        tts.on_speech_end.append(lambda: music.unduck("speech"))
        audio.start()
        power.start()  # on battery: close the mic when nobody's around
        speak(_greeting())

    print(f"\n{'='*50}")
    print("  JARVIS is ready" + ("  (headless brain)" if config.HEADLESS
                                 else "  (background mode)" if config.BACKGROUND_MODE else ""))
    print(f"  Dashboard:  {dash_url}")
    if phone_url:
        tls_note = "" if server.tls["cert"] else " (no HTTPS — mic may be blocked; run 'tailscale cert' manually)"
        print(f"  Phone mic:  {phone_url}{tls_note}")
    else:
        print("  Phone mic:  Tailscale not detected — install Tailscale for remote phone access")
    if config.PHONE_PUSH:
        print(f"  Phone alerts: ntfy app -> subscribe to \"{phone_topic}\" on {config.NTFY_SERVER}")
    print(f"  Workspaces: {list(state.workspaces.keys()) or 'none'}")
    print(f"  AI: {config.MODEL}   OS: {config.OS}")
    print(f"  Input folder:  {config.JARVIS_INPUT_DIR}")
    print(f"  Output folder: {config.JARVIS_OUTPUT_DIR}")
    print(f"{'='*50}")
    if config.HEADLESS:
        print("\n  Waiting for commands from the phone page.\n")
    else:
        print("\n  Say 'Jarvis' to activate — or all at once: 'Jarvis, open Discord'.\n")


# An answer that means "never mind" ends the exchange instead of going to Claude
_END_ANSWERS = {"never mind", "nevermind", "forget it", "cancel", "nothing", "no thanks", "no thank you",
                "that's all", "that's it", "not now", "skip it"}
_MAX_FOLLOWUPS = 3   # questions answered in a row without the wake word
_RETRY = "I didn't catch that. Say it again?"
# A photo from the phone goes to Claude with the question; this tells it what the image is
_PHOTO_NOTE = ("(Photo from my phone attached. Answer about it as chat unless I ask for an action — "
               "add it to my agenda, remember it, find it nearby…) ")


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
    heard = audio.listen_for_command(onset_timeout=config.ANSWER_WINDOW)
    answer = re.sub(r"^\W*(hey |ok |okay )?jarvis\b\W*", "", heard or "", flags=re.IGNORECASE).strip()
    if not answer and audio._WAKE_ANYWHERE.search(heard or ""):
        # Just "Jarvis" instead of an answer: a fresh activation, not a reply
        checkins.unanswered(question)
        speak(random.choice(_ACKS))
        wait_tts()
        command = audio.listen_for_command()
        return voice.screen(command, "prompted") if command else None
    if not answer or answer.lower().rstrip(".!?") in _END_ANSWERS:
        checkins.unanswered(question, dismissed=bool(answer))   # "not now" → never asked again
        return None
    answer = voice.screen(answer, "answer")      # someone else answering → "Who is this?"
    if not answer:
        return None
    if question == _RETRY:       # a second try at the same command, not an answer
        return answer
    checkins.answered(question, answer)
    return f'(Answering your question "{question}") {answer}'


def _next_command():
    """Block until the wake word or a phone command. Returns (command, from_phone)."""
    audio.enable_wake()
    waiting_since = time.time()
    while not audio.activated.wait(timeout=0.1):
        if state.shutdown.is_set():   # the desktop window was closed
            return None, False
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

    speak(random.choice(_ACKS))
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


def _window_closed():
    """You closed the desktop app: shut down, as if you'd said "shut down" (the main loop
    notices between commands)."""
    stopped = coding.stop_all()
    if stopped:
        print(f"  Stopped Claude Code: {', '.join(stopped)}")
    speak("Shutting down. Goodbye, sir.")
    state.shutdown.set()


def _shutdown():
    """Finish saying goodbye, save the Stage, close the desktop app."""
    audio.disable_wake()
    music.stop()
    stage.flush()
    wait_tts()
    desktop.close()
    print("\n  JARVIS shutting down. Goodbye.")


def _stop_signal(signum, frame):
    raise KeyboardInterrupt   # systemctl stop / restart: the same clean exit as Ctrl+C


def main():
    if not config.WINDOWS:
        signal.signal(signal.SIGTERM, _stop_signal)
    _startup()
    followup, followups = None, 0
    while True:
        try:
            if state.shutdown.is_set():  # the desktop window was closed
                _shutdown()
                break
            if followup:                 # the answer to a question Jarvis just asked
                command, from_phone, followup = followup, False, None
                followups += 1
            else:
                print("Waiting for wake-word or phone command...")
                command, from_phone = _next_command()
                followups = 1 if isinstance(command, str) and command.startswith("(Answering your question") else 0
            image = None
            if isinstance(command, dict):   # a photo from the phone, with an optional question
                image, command = command["image"], command["text"] or "What is this?"
                stage.add_image(image, caption=command, title="Photo from phone")
                command = _PHOTO_NOTE + command
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
            shown = re.sub(r'^\(Answering your question ".*?"\) ', "", command)
            state.push_state("thinking", transcript=f"[Photo] {command[len(_PHOTO_NOTE):]}" if image else shown,
                             sample="" if from_phone or fixed else voice_id.current.get("sample", ""))
            if verbosity.update(command):
                print("  Detailed answer requested.")
            if not image and should_bypass_ai(command):
                print("  Bypassed AI (dismissal command).")
                _finish()
                continue

            checkins.before_turn(command, from_phone, followups)   # back after a break / small talk?
            response = None if image else router.route(command)
            if response:
                print("  Handled locally (no AI call).")
            else:
                print("  Thinking...")
                response = brain.ask_claude(command, state.workspaces, image=image)
            if voice_id.current.get("guest") and (response or {}).get("mode") == "action":
                response = voice.guest_reply()   # strangers may chat, not act
            response = checkins.shape_reply(response)   # one question at a time

            audio.enable_wake()   # let the user interrupt Jarvis mid-response
            try:
                actions.handle_response(response)
            except Exception as e:
                print(f"  Error in handle_response: {e}")
            if state.shutdown.is_set():          # "good night" / "shut down"
                _shutdown()
                break
            # a natural moment (back after a break, small talk)? ask how something important went
            if not audio.activated.is_set():
                try:
                    checkins.after_turn(response, turn_start)
                except Exception as e:
                    print(f"  Check-in error: {e}")
            followup = _finish(turn_start, followups)

        except KeyboardInterrupt:
            speak("Shutting down. Goodbye.")
            _shutdown()
            break
        except Exception as e:
            print(f"  Unexpected main loop error: {e}")
            state.push_state("idle")
            time.sleep(0.5)


if __name__ == "__main__":
    main()
    os._exit(0)   # don't wait on background threads (a voice download, a stuck request)
