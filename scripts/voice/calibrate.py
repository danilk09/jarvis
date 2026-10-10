"""
Pick voice ID thresholds from your own data. Run after a few days in shadow mode
(VOICE_ID_MODE = "shadow"), once Jarvis has scored a few dozen of your commands.

    python scripts/voice/calibrate.py

Your scores: labeled recordings (you said them — enrollment, ✓/✎ in the dashboard, label.py)
plus the leave-one-out scores of your enrollment. Recordings Jarvis scored but nobody labeled
are shown separately: anything low there is probably someone else, the TV, or noise.

To see where other people land, have a friend say "Jarvis, do you recognize me?".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np                         # noqa: E402

from core import config, speech_data, voice_id   # noqa: E402


def histogram(values, lo=-0.1, hi=1.0, step=0.05):
    bins = np.arange(lo, hi + step, step)
    counts, _ = np.histogram(np.clip(values, lo, hi - 1e-6), bins)
    peak = max(counts.max(), 1)
    for b, c in zip(bins, counts):
        if c:
            mark = " <ACCEPT" if b <= config.VOICE_ACCEPT < b + step else " <REJECT" if b <= config.VOICE_REJECT < b + step else ""
            print(f"  {b:5.2f}  {'#' * max(1, int(40 * c / peak)):<40} {c}{mark}")


def main():
    if not voice_id.available() or not voice_id.enrolled():
        sys.exit('Not enrolled yet: say "Jarvis, learn my voice" first.')
    samples = [r for r in speech_data.load_samples().values() if r.get("score") is not None]
    mine = [r["score"] for r in samples if r.get("text")] + [s for s in voice_id.self_scores() if s is not None]
    unknown = [r["score"] for r in samples if not r.get("text")]
    print(f"Current: ACCEPT {config.VOICE_ACCEPT:.2f}, REJECT {config.VOICE_REJECT:.2f}, mode {voice_id.mode()}\n")
    for mic, n in voice_id.enrolled_mics().items():
        print(f"  enrolled on {mic}: {n}")

    if mine:
        mine = np.array(mine)
        print(f"\nYour voice ({len(mine)} scores): median {np.median(mine):.2f}, lowest {mine.min():.2f}")
        histogram(mine)
    if unknown:
        print(f"\nUnlabeled recordings ({len(unknown)}):")
        histogram(np.array(unknown))

    if len(mine) < 20:
        print("\nToo few labeled scores to suggest thresholds — label some commands (dashboard ✓ or label.py).")
        return
    # ~95% of your commands pass on the first try, kept in a range that suits this model:
    # strangers score ~0-0.3, and short commands score lower than these mostly-longer samples
    accept = float(np.clip(np.percentile(mine, 5) - 0.05, 0.35, 0.60))
    reject = float(np.clip(min(accept - 0.15, np.percentile(mine, 1) - 0.10), 0.15, 0.35))
    print(f"\nSuggested (core/config.py):\n  VOICE_ACCEPT = {accept:.2f}\n  VOICE_REJECT = {reject:.2f}")
    print("Other people usually score below 0.3 on this model. If a friend scored above your\n"
          "REJECT, raise it; if Jarvis often asks you to repeat, lower ACCEPT a little.")


if __name__ == "__main__":
    main()
