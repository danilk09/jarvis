"""
Which speech-to-text model hears you best on this PC? Runs every labeled recording in
jarvis_memory/voice/ (enrollment lines, corrections, ✓ in the dashboard, label.py) through
each model with exactly Jarvis's settings, and reports word error rate and speed.

    python scripts/voice/benchmark.py                          # base.en small.en distil-small.en
    python scripts/voice/benchmark.py base.en large-v3-turbo   # pick models
    python scripts/voice/benchmark.py models/whisper-me        # a fine-tuned model folder
    python scripts/voice/benchmark.py --no-prompt base.en      # without the vocabulary prompt
    python scripts/voice/benchmark.py --beam 5 small.en        # beam search instead of greedy
    python scripts/voice/benchmark.py --show small.en          # print every mistake

Then set WHISPER_MODEL (and WHISPER_BEAM) in core/config.py. Lower WER is better; more
than ~1.5 s per command starts to feel slow.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from faster_whisper import WhisperModel   # noqa: E402

from core import asr, files, speech_data, state   # noqa: E402

state.workspaces.update(files.load_workspaces())   # workspace names go in the prompt, as in Jarvis

DEFAULT_MODELS = ["base.en", "small.en", "distil-small.en"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--no-prompt", action="store_true", help="decode without the hint prompt")
    ap.add_argument("--beam", type=int, default=None, help="beam size (default: WHISPER_BEAM)")
    ap.add_argument("--show", action="store_true", help="print each recording that was wrong")
    ap.add_argument("--limit", type=int, default=0, help="only the newest N recordings")
    args = ap.parse_args()

    samples = sorted(speech_data.labeled(), key=lambda r: r["time"])
    if args.limit:
        samples = samples[-args.limit:]
    if not samples:
        sys.exit("No labeled recordings yet. Say \"Jarvis, learn my voice\", correct a few transcripts in the\n"
                 "dashboard (✎ / ✓), or run scripts/voice/label.py first.")
    audio = [speech_data.read_wav(os.path.join(speech_data.SAMPLES_DIR, r["wav"])) for r in samples]
    seconds = sum(len(a) for a in audio) / 16000
    print(f"{len(samples)} labeled recordings, {seconds / 60:.1f} min of audio\n")

    results = []
    for name in args.models:
        print(f"{name}: loading...", end="", flush=True)
        try:
            model = WhisperModel(name, device="cpu", compute_type="int8")
        except Exception as e:
            print(f" failed: {e}")
            continue
        asr.decode(model, audio[0], use_prompt=not args.no_prompt, beam_size=args.beam)   # warm-up
        errors = words = 0
        wrong = []
        start = time.perf_counter()
        for rec, a in zip(samples, audio):
            text, _ = asr.transcribe(model, a, use_prompt=not args.no_prompt, beam_size=args.beam)
            e, n = speech_data.word_errors(rec["text"], text)
            errors += e
            words += n
            if e:
                wrong.append((rec["text"], text))
        per = (time.perf_counter() - start) / len(samples)
        wer = errors / max(words, 1)
        results.append((name, wer, per, len(wrong)))
        print(f"\r{name}: WER {wer:6.1%}   {per:5.2f} s per command   {len(wrong)}/{len(samples)} with mistakes")
        if args.show:
            for ref, hyp in wrong:
                print(f"    said:  {ref}\n    heard: {hyp}\n")

    if len(results) > 1:
        print("\nBest first:")
        for name, wer, per, n in sorted(results, key=lambda r: r[1]):
            print(f"  {name:<28} WER {wer:6.1%}   {per:5.2f} s")


if __name__ == "__main__":
    main()
