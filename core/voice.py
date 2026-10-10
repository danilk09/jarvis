"""
Voice recognition in the conversation: the speaker check on every command, the "who is
this?" challenge, enrollment ("Jarvis, learn my voice") and the `voice` action.

screen(command, source) runs on every spoken command (jarvis.py):
  - you (or voice ID off / in shadow mode)  → the command goes ahead; its recording is saved
  - unsure                                  → "Say it again?" and the repeat is checked
  - someone else, straight after the wake word → ignored, silently
  - someone else when Jarvis was made to listen (orb click, after "Yes sir", answering a
    question) → "I don't recognize your voice. Who is this?", logged, refused
  - a microphone you never enrolled on      → allowed, with a one-time hint to enroll it
VOICE_GUEST_MODE lets strangers chat instead (no actions).
"""

import re
import time

from . import agent, audio, config, speech_data, stage, state, voice_id
from .registry import action
from .tts import speak, wait_tts

_hinted_mics: set = set()


def start():
    """Load the speaker model, report the state, prune old recordings (all in the background)."""
    import threading

    def run():
        if voice_id.available():
            mics = voice_id.enrolled_mics()
            if mics:
                print(f"  Voice ID: {voice_id.mode()} mode, {sum(mics.values())} enrolled samples on "
                      f"{len(mics)} microphone{'s' if len(mics) != 1 else ''}")
            else:
                print('  Voice ID: not enrolled yet — say "Jarvis, learn my voice"')
        try:
            speech_data.prune()
        except Exception as e:
            print(f"  [voice] prune failed: {e}")
    threading.Thread(target=run, daemon=True, name="voice-start").start()


def _owner():
    return voice_id.owner_name() or "my owner"


def _record(source, result):
    """Save the last recording (not a refused stranger's) and publish who's talking."""
    cap = audio.last_capture
    sid = ""
    if cap["frames"] and not (result["verdict"] == "other" and voice_id.enforcing()):
        heard = cap["text"]
        sid = speech_data.save_sample(cap["frames"], heard, cap["raw"], source=source, mic=audio.mic["name"],
                                      score=result["score"], verdict=result["verdict"],
                                      prefix=_wake_prefix(heard) if source == "wake" else None)
    voice_id.current.clear()
    voice_id.current.update({k: v for k, v in result.items() if k != "vec"}, sample=sid, guest=False)
    return sid


def _wake_prefix(heard):
    """The part of a one-breath transcript up to the command ("Jarvis, ")."""
    m = audio._WAKE_ANYWHERE.search(heard)
    return heard[:m.end()] if m else ""


def _strip_wake(text):
    return re.sub(r"^\W*(hey |ok |okay )?jarvis\b\W*", "", text or "", flags=re.IGNORECASE).strip()


def screen(command, source):
    """
    Check who said `command` (the last recording). source: "wake" (one breath with the wake
    word), "prompted" (after "Yes sir"), "dashboard" (orb click), "answer" (answering Jarvis).
    Returns the command to run, or None.
    """
    cap = audio.last_capture
    frames = cap["frames"][cap.get("voice_from", 0):]
    if source == "prompted":
        frames = audio.wake_audio() + frames       # "Jarvis" itself helps a short command
    mic = audio.mic["name"]
    result = voice_id.check(frames, mic)
    _record(source, result)
    if result["score"] is not None:
        print(f"  {voice_id.describe(result)}")
    verdict = result["verdict"]

    if verdict == "owner":
        voice_id.adapt(result, mic)
        return command
    if verdict == "new_mic":
        if voice_id.enforcing() and mic not in _hinted_mics:
            _hinted_mics.add(mic)
            speak("I don't know your voice on this microphone yet. Say 'learn my voice' sometime to add it.")
        return command
    if verdict == "none" or not voice_id.enforcing():
        return command

    if verdict == "unsure":
        speak("Sorry, I didn't quite catch that. Say it again?")
        wait_tts()
        again = _strip_wake(audio.listen_for_command(onset_timeout=config.ANSWER_WINDOW))
        if not again:
            return None
        result = voice_id.check(audio.last_capture["frames"], mic)
        _record("repeat", result)
        print(f"  {voice_id.describe(result)}")
        if result["verdict"] in ("owner", "new_mic", "none"):
            return again
        command = again

    # Someone else
    if config.VOICE_GUEST_MODE:
        voice_id.current["guest"] = True
        print("  Guest: chat only.")
        return command
    if source == "wake":
        print("  Ignoring: not your voice.")
        return None
    _challenge(command, result)
    return None


def wake_is_stranger():
    """Before "Yes sir": True if the wake word itself clearly wasn't you (enforce mode only)."""
    if not voice_id.enforcing() or config.VOICE_GUEST_MODE or audio.wake_source() != "voice":
        return False
    result = voice_id.check(audio.wake_audio(), audio.mic["name"])
    if result["verdict"] == "other":
        print(f"  Ignoring the wake word: not your voice ({voice_id.describe(result)})")
        return True
    return False


def answer_allowed():
    """For yes/no confirmations: False only when voice ID is enforcing and it clearly wasn't you."""
    if not voice_id.enforcing():
        return True
    result = voice_id.check(audio.last_capture["frames"], audio.mic["name"])
    if result["verdict"] == "other":
        print(f"  Confirmation ignored: not your voice ({voice_id.describe(result)})")
        return False
    return True


def guest_reply():
    return {"mode": "chat", "reply": f"Sorry, only {_owner()} can ask me to do that."}


_NAME_LEAD = re.compile(r"^\W*(?:(?:hi|hey|hello|yeah|oh|um|uh|jarvis)\W+)*"
                        r"(?:it'?s|it is|this is|i'?m|i am|my name is|my name's|the name is)?\W*", re.IGNORECASE)


def extract_name(reply):
    """ "Hi, it's Sarah." → "Sarah" """
    rest = _NAME_LEAD.sub("", reply or "").strip()
    words = re.findall(r"[A-Za-z][\w'-]*", rest)[:2]
    if not words or words[0].lower() in {"nobody", "no", "none", "never", "nothing", "not"}:
        return ""
    if len(words) == 2 and words[1].lower() in {"here", "speaking", "and", "from", "i"}:
        words = words[:1]
    return " ".join(w.capitalize() for w in words)


def _challenge(said, result):
    owner = _owner()
    speak("I don't recognize your voice. Who is this?")
    wait_tts()
    reply = audio.listen_for_command(onset_timeout=config.ANSWER_WINDOW)
    name = extract_name(reply)
    voice_id.log_visitor(name, said, result)
    print(f'  Visitor: {name or "unknown"} (said "{said}")')
    if name and speech_data.norm(name).split()[0] == speech_data.norm(owner).split()[0]:
        speak(f"You don't sound like {owner} to me. Sorry, I can't help.")
    elif name:
        speak(f"Nice to meet you, {name}. Sorry, I only take commands from {owner}.")
    else:
        speak(f"Sorry, I only take commands from {owner}.")


# ── Enrollment ────────────────────────────────────────────────────────────────
# Mixed on purpose: the wake word alone (verification is most accurate on a known word),
# commands as you'd say them, and a few long lines for the voiceprint.
ENROLL_LINES = [
    "Jarvis",
    "Jarvis, open Discord",
    "What's the weather like tomorrow?",
    "Jarvis",
    "Play some lo-fi music",
    "Add a dentist appointment next Tuesday at three",
    "Jarvis, what time is it?",
    "Search YouTube for the best pasta recipe",
    "Jarvis",
    "Take a screenshot and tell me what's on my screen",
    "Remind me to call my mom at seven tonight",
    "Skip this song and turn the volume down",
    "Jarvis",
    "Give me a breakdown of today",
    "Find pizza restaurants near me",
    "Jarvis, never mind",
    "What do you remember about me?",
    "Show me a map of Tokyo",
    "Jarvis",
    "Code me a to-do list app with dark mode",
    "Read me the top headlines",
    "Jarvis, pause the music",
    "Set a timer for twenty minutes",
    "What's on my agenda this week?",
    "Jarvis",
    "Open Visual Studio Code",
    "How far is the moon from the Earth?",
    "Jarvis, resume",
    "The quick brown fox jumps over the lazy dog",
    "I'd like you to recognize my voice, even when the room is noisy",
]
_STOP_WORDS = re.compile(r"^\W*(stop|cancel|that's enough|enough|i'm done|quit)\W*$", re.IGNORECASE)


def _lines():
    """The script plus "open <workspace>" for each workspace (names Whisper should learn)."""
    return ENROLL_LINES + [f"Jarvis, open {name}" for name in list(state.workspaces)[:4]]


def _matches(expected, heard):
    """Close enough that this recording is the line (not noise or someone else talking)."""
    exp, got = speech_data.norm(expected).split(), speech_data.norm(heard).split()
    if not got:
        return False
    if exp == ["jarvis"]:
        return bool(audio._WAKE_ANYWHERE.search(heard)) and len(got) <= 3
    errors, total = speech_data.word_errors(expected, heard)
    return errors / max(total, 1) <= 0.5


def _progress_md(lines, done, current):
    out = ["# Voice training", "",
           "Read the line marked ▶ out loud, at your normal volume and distance. "
           "Say **stop** to finish early — what you've read so far is kept.", ""]
    for i, line in enumerate(lines):
        mark = "✓" if i in done else "▶" if i == current else "·"
        text = f"**{line}**" if i == current else line
        out.append(f"{mark} Line {i + 1} — {text}  ")
    return "\n".join(out)


def enroll():
    """Read the script line by line; each good recording becomes a voiceprint and labeled data."""
    if not voice_id.available():
        speak("Voice recognition isn't installed. Run the setup script first.")
        return "Voice ID unavailable"
    if voice_id.enrolled() and voice_id.current.get("verdict") == "other":
        speak("You don't sound like the voice I know, so I can't add you.")
        return "Enrollment refused: not the owner"
    audio.disable_wake()          # the script says "Jarvis" a lot
    lines, mic = _lines(), audio.mic["name"]
    done = set()
    pid = stage.add("note", "Voice training", {"markdown": _progress_md(lines, done, 0)}, focus=True)
    speak("Let's learn your voice. Read each marked line on the stage when you're ready. Say stop to finish early.")
    wait_tts()
    silent = 0
    for i, line in enumerate(lines):
        stage.update(pid, markdown=_progress_md(lines, done, i))
        for attempt in range(3):
            heard = audio.listen_for_command(max_duration=10, silence_duration=0.9, onset_timeout=12)
            if heard and _STOP_WORDS.match(heard):
                return _enroll_done(pid, lines, done, mic, stopped=True)
            if not heard:
                silent += 1
                if silent >= 2:
                    speak("I'll stop here. Say 'learn my voice' again to keep going.")
                    return _enroll_done(pid, lines, done, mic, stopped=True, quiet=True)
                speak(f"Whenever you're ready: line {i + 1}.")
                wait_tts()
                continue
            silent = 0
            cap = audio.last_capture
            if not _matches(line, heard):
                print(f'  [enroll] line {i + 1}: heard "{heard}" — doesn\'t match, retrying')
                if attempt < 2:
                    speak("Once more?")
                    wait_tts()
                continue
            vec, seconds = voice_id.embed(cap["frames"])
            if vec is None:
                continue
            voice_id.add_enrollment(vec, mic)
            speech_data.save_sample(cap["frames"], heard, cap["raw"], source="enroll", text=line,
                                    label_source="script", mic=mic)
            done.add(i)
            print(f"  [enroll] line {i + 1}/{len(lines)} ok ({seconds:.1f} s)")
            break
    return _enroll_done(pid, lines, done, mic)


def _enroll_done(pid, lines, done, mic, stopped=False, quiet=False):
    stage.update(pid, markdown=_progress_md(lines, done, -1))
    scores = [s for s in voice_id.self_scores() if s is not None]
    consistency = f" Consistency {sum(scores) / len(scores):.2f}." if scores else ""
    print(f"  [enroll] {len(done)} lines saved on {mic}.{consistency}")
    if not quiet:
        if len(done) >= 8:
            speak(f"Got it. I've learned your voice from {len(done)} lines on this microphone."
                  + (" Say 'turn on voice lock' when you want me to ignore other people."
                     if voice_id.mode() != "enforce" else ""))
        elif done:
            speak(f"I saved {len(done)} lines. A few more would help — say 'learn my voice' to continue.")
        else:
            speak("I didn't get any usable recordings.")
    return f"Voice enrollment: {len(done)} lines on {mic}" + (" (stopped early)" if stopped else "")


# ── The voice action ──────────────────────────────────────────────────────────
def _test():
    speak("Say a sentence or two in your normal voice.")
    wait_tts()
    heard = audio.listen_for_command(max_duration=8, onset_timeout=8)
    if not heard:
        speak("I didn't hear anything.")
        return "Voice test: nothing heard"
    result = voice_id.check(audio.last_capture["frames"], audio.mic["name"])
    if result["score"] is None:
        speak("That was too short to tell." if voice_id.enrolled() else "I haven't learned your voice yet.")
        return "Voice test: no score"
    pct = round(result["score"] * 100)
    speak({"owner": f"That's you. {pct} percent match.",
           "unsure": f"I'm not sure. {pct} percent match.",
           "other": f"That doesn't sound like you. {pct} percent match.",
           "new_mic": f"{pct} percent match, but I've never heard you on this microphone."}[result["verdict"]])
    return f"Voice test: {voice_id.describe(result)}"


def _status():
    if not voice_id.available():
        speak("Voice recognition isn't installed.")
        return "Voice ID unavailable"
    mics, c = voice_id.enrolled_mics(), voice_id.counts()
    labeled = len(speech_data.labeled())
    fixes = sum(p["count"] >= config.VOICE_RULE_MIN for p in speech_data.corrections())
    if mics:
        speak(f"Voice lock is {'on' if voice_id.mode() == 'enforce' else 'off'}. I know your voice from "
              f"{c['enroll']} recordings on {len(mics)} microphone{'s' if len(mics) != 1 else ''}, plus "
              f"{c['adapt']} learned since. I have {labeled} labeled recordings and {fixes} automatic fixes.")
    else:
        speak(f"I haven't learned your voice yet. Say 'learn my voice'. I have {labeled} labeled recordings.")
    return f"Voice status: {voice_id.mode()}, {c}, {labeled} labeled, {fixes} fixes"


def _show_words():
    vocab = speech_data.vocabulary()
    pairs = speech_data.corrections()
    md = ["# Voice vocabulary", "",
          f"Words Whisper is told to expect. Edit `{speech_data.VOCAB_FILE}` to change them.", ""]
    md += [f"- {w}" for w in vocab] or ["_None yet — say “learn the word …”._"]
    md += ["", "## Corrections", "",
           f"Fixed automatically once corrected {config.VOICE_RULE_MIN}+ times.", "",
           "| Heard | Meant | Times |", "|---|---|---|"]
    md += [f"| {p['heard']} | {p['meant']} | {p['count']}{' ✓' if p['count'] >= config.VOICE_RULE_MIN else ''} |"
           for p in pairs] or ["| — | — | — |"]
    stage.add("note", "Voice vocabulary", {"markdown": "\n".join(md)})
    speak("Your voice vocabulary is on the stage.")
    return f"Voice vocabulary: {len(vocab)} words, {len(pairs)} corrections"


@action("voice",
        schema='{"type":"voice","command":"enroll|test|status|lock_on|lock_off|reset|add_word|show_words","word":"<for add_word>"}',
        rules=['"learn/train my voice", "add this microphone" → voice "enroll"; "do you recognize me", '
               '"test my voice" → "test"; "turn on/off voice lock", "only listen to me" → "lock_on"/"lock_off"; '
               '"forget my voice" → "reset"; "learn the word X", "X is spelled …" → "add_word" with word X; '
               '"show the words you know", "show my voice corrections" → "show_words"'])
def _voice(a, chain):
    cmd = (a.get("command") or "status").lower()
    if cmd == "enroll":
        return enroll()
    if cmd == "test":
        return _test()
    if cmd == "status":
        return _status()
    if cmd == "lock_on":
        if not voice_id.enrolled():
            speak("I need to learn your voice first. Say 'learn my voice'.")
            return "Voice lock: not enrolled"
        voice_id.set_mode("enforce")
        speak("Voice lock is on. I'll only take commands from you.")
        return "Voice lock on"
    if cmd == "lock_off":
        voice_id.set_mode("shadow")
        speak("Voice lock is off. I'll listen to anyone, but keep checking in the background.")
        return "Voice lock off"
    if cmd == "reset":
        if voice_id.current.get("verdict") == "other":
            speak("You don't sound like the voice I know, so I won't do that.")
            return "Voice reset refused"
        if not agent.confirm("This deletes the voice profile I've learned"):
            speak("Okay, I'll keep it.")
            return "Voice reset cancelled"
        voice_id.reset()
        voice_id.set_mode("shadow")
        speak("Done. Say 'learn my voice' to start over.")
        return "Voice profile reset"
    if cmd == "add_word":
        word = (a.get("word") or "").strip()
        if not word:
            speak("Which word?")
            return "add_word: no word"
        added = speech_data.add_word(word)
        speak(f"Got it, I'll listen for {word}." if added else f"I already know {word}.")
        return f"Vocabulary: {word}"
    if cmd == "show_words":
        return _show_words()
    speak("I'm not sure what to do with my voice settings.")
    return f"voice: unknown command {cmd}"
