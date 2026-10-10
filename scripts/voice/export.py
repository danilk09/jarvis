"""
Package your labeled recordings for fine-tuning Whisper on a GPU (see finetune.py).

    python scripts/voice/export.py          # → jarvis_voice_data.zip in the current folder

The zip holds audio/<id>.wav, metadata.jsonl ({"audio", "text", "split"}) with ~10% held out
for testing, and prompt.txt (Jarvis's hint prompt, so training matches how Jarvis decodes).
It contains your voice: keep it private, and delete it from Colab/Drive when you're done.
"""

import argparse
import io
import json
import os
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import asr, files, speech_data, state   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="jarvis_voice_data.zip")
    args = ap.parse_args()

    samples = sorted(speech_data.labeled(), key=lambda r: r["time"])
    if not samples:
        sys.exit("No labeled recordings yet.")
    state.workspaces.update(files.load_workspaces())
    meta = io.StringIO()
    seconds = 0.0
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as z:
        for r in samples:
            path = os.path.join(speech_data.SAMPLES_DIR, r["wav"])
            seconds += os.path.getsize(path) / 32000
            z.write(path, f"audio/{r['id']}.wav")
            # Stable split: the same recording always lands on the same side
            split = "test" if zlib.crc32(r["id"].encode()) % 10 == 0 else "train"
            meta.write(json.dumps({"audio": f"audio/{r['id']}.wav", "text": r["text"], "split": split}) + "\n")
        z.writestr("metadata.jsonl", meta.getvalue())
        z.writestr("prompt.txt", ", ".join(asr.phrases()))
    print(f"Wrote {args.out}: {len(samples)} recordings, {seconds / 60:.1f} min of audio.")
    if seconds < 20 * 60:
        print("Under 20 minutes: fine-tuning will help a little at best. 30-60 minutes is the sweet spot.")


if __name__ == "__main__":
    main()
