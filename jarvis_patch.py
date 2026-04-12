# ── JARVIS Speaker Auth Patch ─────────────────────────────────────────────────
# Apply these 4 changes to jarvis.py. Each section shows FIND → REPLACE.
# ─────────────────────────────────────────────────────────────────────────────


# ════════════════════════════════════════════════════════════════════════════
# CHANGE 1 — Add import near the top (after `import threading`)
# ════════════════════════════════════════════════════════════════════════════

# ADD this line after the existing `import threading` at the top:

from speaker_auth import SpeakerAuth


# ════════════════════════════════════════════════════════════════════════════
# CHANGE 2 — Initialise SpeakerAuth after WHISPER_MODEL is loaded
# ════════════════════════════════════════════════════════════════════════════

# FIND:
#   print("  Loading Whisper model (first run may take a moment)...")
#   WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")

# REPLACE WITH:
print("  Loading Whisper model (first run may take a moment)...")
WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")

SPEAKER_AUTH = SpeakerAuth()   # ← ADD THIS LINE


# ════════════════════════════════════════════════════════════════════════════
# CHANGE 3 — Tighter noise gate + speaker verification in listen_for_command
# ════════════════════════════════════════════════════════════════════════════

# FIND the block inside listen_for_command that starts with:
#   silence_threshold = max(_ambient_level[0] * 4.0, 0.02)
# and ends before the `return text` at the very bottom of the function.
#
# REPLACE the entire function body from `silence_threshold = ...`
# down through `return ""` (the final except block) with the version below.
# The function signature `def listen_for_command(...)` stays the same.

def listen_for_command(max_duration=10, silence_duration=2.0):
    # Drain any audio captured during TTS playback
    time.sleep(0.05)
    with _audio_lock:
        _audio_buffer.clear()

    # ── Tighter noise gate (was *4.0, now *6.0 + higher floor) ──────────────
    silence_threshold = max(_ambient_level[0] * 6.0, 0.035)
    print(f"  Speak your command... (noise floor: {silence_threshold:.3f})")

    with _audio_lock:
        _audio_buffer.clear()

    silence_chunks_needed = int((SAMPLE_RATE / CHUNK) * silence_duration)
    max_chunks = int((SAMPLE_RATE / CHUNK) * max_duration)

    _capture_active.set()

    silent_chunks    = 0
    speaking_started = False
    chunk_count      = 0
    # Require at least 0.4s of actual speech before treating it as a command
    MIN_SPEAKING_CHUNKS = int((SAMPLE_RATE / CHUNK) * 0.4)
    speaking_chunks  = 0

    while True:
        time.sleep(0.01)
        with _audio_lock:
            chunk_count = len(_audio_buffer)
            if chunk_count == 0:
                continue
            last_chunk = _audio_buffer[-1]

        volume = float(np.abs(last_chunk).max())

        if volume > silence_threshold * 2.5:
            speaking_started = True
            speaking_chunks += 1
            silent_chunks    = 0
        elif speaking_started:
            silent_chunks += 1

        if chunk_count >= max_chunks or (
            speaking_started
            and speaking_chunks >= MIN_SPEAKING_CHUNKS
            and silent_chunks >= silence_chunks_needed
        ):
            break

    _capture_active.clear()

    with _audio_lock:
        frames = list(_audio_buffer)

    if not frames or not speaking_started or speaking_chunks < MIN_SPEAKING_CHUNKS:
        print("  No speech detected.")
        return ""

    recording = np.concatenate(frames, axis=0)

    # ── Speaker verification ─────────────────────────────────────────────────
    # Resample to 16 kHz for the speaker model (recording is at SAMPLE_RATE)
    try:
        import soxr
        audio_16k = soxr.resample(recording.flatten(), SAMPLE_RATE, 16000)
    except ImportError:
        # Fallback: simple decimation (good enough for verification)
        step = SAMPLE_RATE // 16000
        audio_16k = recording.flatten()[::step]

    is_known, who = SPEAKER_AUTH.verify(audio_16k, sample_rate=16000)
    if not is_known:
        print("  [SpeakerAuth] Voice not recognised — ignoring audio.")
        return ""

    tmp_path = os.path.join(tempfile.gettempdir(), "_jarvis_tmp.wav")
    sf.write(tmp_path, recording, SAMPLE_RATE)

    print("  Processing speech...")
    try:
        segments, _ = WHISPER_MODEL.transcribe(
            tmp_path,
            language="en",
            vad_filter=True,
            initial_prompt="Open Discord, search YouTube for, open workspace, never mind, stop, yes, no, cancel, let's talk, back to commands, open file, find files, Danil",
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ))
        text = " ".join(s.text for s in segments).strip()
        if text:
            print(f'  Heard: "{text}"')
        else:
            print("  Could not understand audio.")
        return text
    except Exception as e:
        print(f"  Whisper error: {e}")
        return ""
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# CHANGE 4 — Enrollment CLI in main()
# ════════════════════════════════════════════════════════════════════════════

# FIND inside main(), right after:
#   workspaces = load_workspaces()

# ADD these lines immediately after:

    # ── Speaker enrollment check ─────────────────────────────────────────────
    if not SPEAKER_AUTH.list_users():
        print("\n  No users enrolled yet.")
        print("  JARVIS will run in open mode (anyone can activate it).")
        print("  To enroll yourself, run:  python jarvis.py --enroll")
    else:
        print(f"  Enrolled users: {SPEAKER_AUTH.list_users()}")


# ════════════════════════════════════════════════════════════════════════════
# CHANGE 5 — --enroll CLI flag at the bottom of main()
# ════════════════════════════════════════════════════════════════════════════

# FIND at the very bottom:
#   if __name__ == "__main__":
#       main()

# REPLACE WITH:

if __name__ == "__main__":
    if "--enroll" in sys.argv:
        # Standalone enrollment mode — no wake word loop needed
        sa = SpeakerAuth()
        sa.wait_until_ready(timeout=60)
        sa.enroll_interactive()
    elif "--remove-user" in sys.argv:
        idx = sys.argv.index("--remove-user")
        name = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if name:
            sa = SpeakerAuth()
            removed = sa.remove_user(name)
            print(f"  {'Removed' if removed else 'User not found'}: {name}")
        else:
            print("  Usage: python jarvis.py --remove-user <name>")
    else:
        main()
