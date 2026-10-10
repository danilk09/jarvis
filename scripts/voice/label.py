"""
Label recordings fast: plays each unlabeled command Jarvis heard and shows its transcript.

    Enter        it heard right
    <text>       what you actually said (teaches Jarvis the fix)
    r            replay
    s            skip (not your voice, noise, can't tell)
    q            quit

    python scripts/voice/label.py            # newest first
    python scripts/voice/label.py --days 3   # only the last 3 days

Labeled recordings are the benchmark (benchmark.py) and the fine-tuning data (export.py).
Run Jarvis or this script, not both: they share the speaker.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sounddevice as sd                  # noqa: E402

from core import speech_data              # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=0)
    args = ap.parse_args()

    cutoff = time.time() - args.days * 86400 if args.days else 0
    todo = [r for r in speech_data.load_samples().values()
            if not r.get("text") and not r.get("skipped") and r["time"] >= cutoff and r.get("verdict") != "other"
            and os.path.exists(os.path.join(speech_data.SAMPLES_DIR, r["wav"]))]
    todo.sort(key=lambda r: -r["time"])
    if not todo:
        sys.exit("Nothing to label.")
    print(__doc__.split("\n\n")[1] + "\n")
    print(f"{len(todo)} recordings to label.\n")

    done = 0
    for i, rec in enumerate(todo, 1):
        audio = speech_data.read_wav(os.path.join(speech_data.SAMPLES_DIR, rec["wav"]))
        when = time.strftime("%b %d %H:%M", time.localtime(rec["time"]))
        score = f'  voice {rec["score"]:.2f}' if rec.get("score") is not None else ""
        print(f'[{i}/{len(todo)}] {when}{score}\n  heard: {rec["heard"]}')
        while True:
            sd.play(audio, 16000)
            answer = input("  > ").strip()
            sd.stop()
            if answer.lower() == "r":
                continue
            break
        if answer.lower() == "q":
            break
        if answer.lower() == "s":
            speech_data._append({"id": rec["id"], "skipped": True})
            continue
        speech_data.label(rec["id"], answer or rec["heard"], "labeler" if answer else "confirmed")
        done += 1
    print(f"\nLabeled {done}. Total labeled: {len(speech_data.labeled())}.")


if __name__ == "__main__":
    main()
