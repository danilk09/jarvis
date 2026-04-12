"""
speaker_auth.py — JARVIS Speaker Verification
Uses SpeechBrain's ECAPA-TDNN model to enroll users and verify speakers.

Install deps:
    pip install speechbrain torch torchaudio

Usage:
    from speaker_auth import SpeakerAuth
    auth = SpeakerAuth()
    auth.enroll_interactive()          # first-time setup
    is_known = auth.verify(audio_np)   # True/False
"""

import os
import json
import time
import numpy as np
import sounddevice as sd
import soundfile as sf
import tempfile
import threading

USERS_FILE    = os.path.join(os.path.dirname(__file__), "users.json")
ENROLL_SAMPLE_RATE = 16000
ENROLL_DURATION    = 5        # seconds per enrollment sample
ENROLL_SAMPLES     = 3        # how many samples to record per user
VERIFY_THRESHOLD   = 0.25     # cosine similarity — lower = stricter (0.0–1.0)
                               # Tune: raise if it rejects you too often, lower if strangers get in


class SpeakerAuth:
    def __init__(self):
        self._model    = None
        self._users    = {}       # {name: [embedding, ...]}
        self._ready    = False
        self._lock     = threading.Lock()
        self._load_users()
        # Load model in background so startup isn't blocked
        threading.Thread(target=self._load_model, daemon=True).start()

    # ── Model ─────────────────────────────────────────────────────────────────

    def _load_model(self):
        try:
            from speechbrain.inference.speaker import SpeakerRecognition
            print("  [SpeakerAuth] Loading ECAPA-TDNN speaker model...")
            self._model = SpeakerRecognition.from_pretrained(
                "speechbrain/spkrec-ecapa-voxceleb",
                savedir="models/speechbrain_spkrec",
                run_opts={"device": "cpu"},
            )
            self._ready = True
            print("  [SpeakerAuth] Speaker model ready.")
        except Exception as e:
            print(f"  [SpeakerAuth] Could not load model: {e}")
            print("  [SpeakerAuth] Run: pip install speechbrain torch torchaudio")

    def wait_until_ready(self, timeout=60):
        """Block until the model finishes loading."""
        deadline = time.time() + timeout
        while not self._ready and time.time() < deadline:
            time.sleep(0.5)
        return self._ready

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_users(self):
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE) as f:
                    raw = json.load(f)
                # Convert lists back to numpy arrays
                self._users = {
                    name: [np.array(e) for e in embeddings]
                    for name, embeddings in raw.items()
                }
                names = list(self._users.keys())
                print(f"  [SpeakerAuth] Loaded {len(names)} enrolled user(s): {names}")
            except Exception as e:
                print(f"  [SpeakerAuth] Could not load users.json: {e}")
                self._users = {}
        else:
            self._users = {}

    def _save_users(self):
        try:
            serialisable = {
                name: [e.tolist() for e in embeddings]
                for name, embeddings in self._users.items()
            }
            with open(USERS_FILE, "w") as f:
                json.dump(serialisable, f)
        except Exception as e:
            print(f"  [SpeakerAuth] Could not save users.json: {e}")

    # ── Embedding ─────────────────────────────────────────────────────────────

    def _embed(self, audio_np: np.ndarray, sample_rate: int = 16000) -> np.ndarray | None:
        """Return a speaker embedding vector for the given audio."""
        if not self._ready or self._model is None:
            return None
        try:
            import torch
            tmp = os.path.join(tempfile.gettempdir(), "_jarvis_spk_tmp.wav")
            # Ensure mono float32
            if audio_np.ndim > 1:
                audio_np = audio_np[:, 0]
            audio_np = audio_np.astype(np.float32)
            sf.write(tmp, audio_np, sample_rate)
            with self._lock:
                embedding = self._model.encode_batch(
                    self._model.load_audio(tmp).unsqueeze(0)
                )
            vec = embedding.squeeze().detach().cpu().numpy()
            os.remove(tmp)
            return vec
        except Exception as e:
            print(f"  [SpeakerAuth] Embedding error: {e}")
            return None

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    # ── Verification ─────────────────────────────────────────────────────────

    def verify(self, audio_np: np.ndarray, sample_rate: int = 16000) -> tuple[bool, str]:
        """
        Check if audio matches any enrolled user.
        Returns (is_known: bool, matched_name: str | "").
        Falls back to True if model isn't loaded yet (fail-open).
        """
        if not self._ready:
            return True, ""   # model still loading — don't block JARVIS

        if not self._users:
            return True, ""   # no users enrolled — open to everyone

        query_emb = self._embed(audio_np, sample_rate)
        if query_emb is None:
            return True, ""   # embedding failed — fail-open

        best_score = -1.0
        best_name  = ""
        for name, embeddings in self._users.items():
            for ref_emb in embeddings:
                score = self._cosine_similarity(query_emb, ref_emb)
                if score > best_score:
                    best_score = score
                    best_name  = name

        matched = best_score >= VERIFY_THRESHOLD
        if matched:
            print(f"  [SpeakerAuth] Verified: {best_name} (score={best_score:.3f})")
        else:
            print(f"  [SpeakerAuth] Unknown speaker (best={best_name}, score={best_score:.3f}) — ignoring.")

        return matched, best_name if matched else ""

    # ── Enrollment ───────────────────────────────────────────────────────────

    def _record_sample(self, index: int) -> np.ndarray | None:
        """Record a single enrollment sample from the microphone."""
        print(f"\n  Sample {index}/{ENROLL_SAMPLES} — speak naturally for {ENROLL_DURATION}s...")
        print("  Recording in 2 seconds...", end="", flush=True)
        time.sleep(2)
        print(" GO!")
        try:
            audio = sd.rec(
                int(ENROLL_DURATION * ENROLL_SAMPLE_RATE),
                samplerate=ENROLL_SAMPLE_RATE,
                channels=1,
                dtype="float32"
            )
            sd.wait()
            return audio.flatten()
        except Exception as e:
            print(f"  Recording error: {e}")
            return None

    def enroll_user(self, name: str) -> bool:
        """
        Enroll a new user by name. Records ENROLL_SAMPLES voice samples.
        Returns True on success.
        """
        if not self.wait_until_ready(timeout=60):
            print("  [SpeakerAuth] Model not ready — cannot enroll.")
            return False

        print(f"\n  ── Enrolling '{name}' ──────────────────────────────")
        print(f"  You'll record {ENROLL_SAMPLES} voice samples of {ENROLL_DURATION}s each.")
        print("  Speak naturally — read anything aloud, describe your day, etc.")

        embeddings = []
        for i in range(1, ENROLL_SAMPLES + 1):
            audio = self._record_sample(i)
            if audio is None:
                print(f"  Sample {i} failed — skipping.")
                continue
            emb = self._embed(audio, ENROLL_SAMPLE_RATE)
            if emb is None:
                print(f"  Could not embed sample {i} — skipping.")
                continue
            embeddings.append(emb)
            print(f"  Sample {i} enrolled ✓")

        if len(embeddings) < 2:
            print("  Not enough good samples — enrollment failed.")
            return False

        self._users[name] = embeddings
        self._save_users()
        print(f"\n  ✅ '{name}' enrolled successfully with {len(embeddings)} samples.")
        return True

    def enroll_interactive(self):
        """Interactive CLI enrollment flow."""
        print("\n  ── JARVIS Speaker Enrollment ──────────────────────────")
        name = input("  Enter your name: ").strip()
        if not name:
            print("  No name entered — cancelled.")
            return
        if name in self._users:
            overwrite = input(f"  '{name}' already enrolled. Re-enroll? (y/n): ").strip().lower()
            if overwrite != "y":
                print("  Cancelled.")
                return
        self.enroll_user(name)

    def list_users(self) -> list[str]:
        return list(self._users.keys())

    def remove_user(self, name: str) -> bool:
        if name in self._users:
            del self._users[name]
            self._save_users()
            print(f"  [SpeakerAuth] Removed user '{name}'.")
            return True
        return False
