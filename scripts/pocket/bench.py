#!/usr/bin/env python3
"""
AirPods bench test for the Mobile Jarvis pocket device (Phase 3.5).

Proves - on the Pi 5 now, on a Zero 2 W later - whether a Raspberry Pi can use AirPods Pro
as Jarvis's mic, speaker and button:

    ./scripts/pocket/bench check     # Bluetooth, PipeWire and the SCO fix are in place
    ./scripts/pocket/bench pair      # pair + connect the AirPods, list their audio profiles
    ./scripts/pocket/bench sound     # music mode (A2DP): can you hear the Pi?
    ./scripts/pocket/bench buttons   # which stem presses reach Linux, left vs right (single/double/triple/hold)
    ./scripts/pocket/bench mic       # headset mode (HFP): record, measure, transcribe, play back
    ./scripts/pocket/bench jarvis    # the real thing: press -> speak -> PC Jarvis -> reply in your ears
    ./scripts/pocket/bench all       # everything above, in order
    ./scripts/pocket/bench report    # print the summary again
    ./scripts/pocket/bench selftest  # offline check of the parsers and verdict (no Bluetooth needed)

Results go to scripts/pocket/results/ (report.md is what to paste back to Claude).
Run as your normal user, not root. Steps that need root (btmon, the SCO fix) call sudo.
"""

import argparse
import json
import math
import os
import platform
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
RESULTS_DIR = os.path.join(HERE, "results")
RESULTS_FILE = os.path.join(RESULTS_DIR, "bench_results.json")
REPORT_FILE = os.path.join(RESULTS_DIR, "report.md")
TTS_DIR = os.path.join(REPO, "models", "tts")
PIPER_VOICE = "vits-piper-en_GB-northern_english_male-medium"

RATE = 16000
TEST_SENTENCE = "Jarvis, what's the weather going to be like in Honolulu tomorrow afternoon?"

# ── small helpers ─────────────────────────────────────────────────────────────

def say(msg=""):
    print(msg, flush=True)


def head(title):
    say()
    say("=" * 64)
    say(f"  {title}")
    say("=" * 64)


def ask(prompt, default=""):
    try:
        ans = input(f"{prompt} ").strip()
    except EOFError:
        ans = ""
    return ans or default


def yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    a = ask(f"{prompt} [{d}]").lower()
    return default if not a else a.startswith("y")


def ask_int(prompt, default, lo, hi):
    """A number in lo..hi; anything else (typo, "4/5", empty) -> the first digit found, else default."""
    m = re.search(r"\d+", ask(prompt, str(default)))
    return max(lo, min(hi, int(m.group()))) if m else default


def run(cmd, timeout=20, sudo=False, input_text=None):
    if sudo:
        cmd = ["sudo"] + cmd
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not installed")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def load_results():
    try:
        with open(RESULTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_results(res):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    res["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)


def mac_us(mac):
    return mac.replace(":", "_").upper()


# ── WAV helpers (stdlib only) ─────────────────────────────────────────────────

def write_wav(path, samples, rate=RATE):
    """samples: bytes of s16le mono."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples)


def tone_wav(path, notes, rate=RATE, vol=0.35):
    """notes: [(freq_hz or 0 for silence, seconds), ...] with short fades."""
    out = bytearray()
    for freq, dur in notes:
        n = int(rate * dur)
        fade = max(1, int(rate * 0.01))
        for i in range(n):
            if freq <= 0:
                v = 0.0
            else:
                env = min(1.0, i / fade, (n - i) / fade)
                v = vol * env * math.sin(2 * math.pi * freq * i / rate)
            out += struct.pack("<h", int(v * 32767))
    write_wav(path, bytes(out), rate)
    return path


def pcm_stats(pcm):
    """RMS dBFS, peak, fraction of exact zeros, seconds."""
    n = len(pcm) // 2
    if n == 0:
        return {"seconds": 0, "rms_dbfs": -120.0, "peak": 0, "zero_frac": 1.0}
    vals = struct.unpack(f"<{n}h", pcm[: n * 2])
    sq = sum(v * v for v in vals) / n
    rms = math.sqrt(sq)
    zeros = sum(1 for v in vals if v == 0) / n
    return {"seconds": round(n / RATE, 2),
            "rms_dbfs": round(20 * math.log10(rms / 32768) if rms > 0 else -120.0, 1),
            "peak": max(abs(v) for v in vals),
            "zero_frac": round(zeros, 3)}


def frame_rms(chunk):
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack(f"<{n}h", chunk[: n * 2])
    return math.sqrt(sum(v * v for v in vals) / n)


# ── Bluetooth / PipeWire wrappers ─────────────────────────────────────────────

def bt(*args, timeout=20):
    return run(["bluetoothctl", *args], timeout=timeout)


def bt_info(mac):
    out = bt("info", mac).stdout
    info = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.strip().split(":", 1)
            info[k.strip()] = v.strip()
    return info


def find_card(mac, out=None):
    """Return {'name', 'profiles': [...], 'active'} for the AirPods' PipeWire card, or None.
    out: `pactl list cards` text (read live when not given)."""
    if out is None:
        out = run(["pactl", "list", "cards"]).stdout
    want = f"bluez_card.{mac_us(mac)}"
    blocks = re.split(r"\n(?=Card #)", out)
    for b in blocks:
        m = re.search(r"Name:\s*(\S+)", b)
        if not m or m.group(1) != want:
            continue
        profiles, active, in_prof = [], "", False
        for line in b.splitlines():
            s = line.strip()
            if s.startswith("Profiles:"):
                in_prof = True
                continue
            if s.startswith("Active Profile:"):
                active = s.split(":", 1)[1].strip()
                in_prof = False
                continue
            if in_prof:
                if re.match(r"^[\w.+-]+:", s) and line.startswith("\t\t"):
                    name = s.split(":", 1)[0]
                    avail = "available: no" not in s
                    profiles.append({"name": name, "available": avail, "desc": s.split(":", 1)[1].strip()[:80]})
                elif not line.startswith("\t\t"):
                    in_prof = False
        return {"name": want, "profiles": profiles, "active": active}
    return None


def node_names(kind, mac, out=None):
    """kind: 'sinks' or 'sources'. Names of the AirPods' nodes (out: `pactl list short <kind>` text)."""
    if out is None:
        out = run(["pactl", "list", "short", kind]).stdout
    key = mac_us(mac)
    names = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and key in parts[1].upper() and not parts[1].endswith(".monitor"):
            names.append(parts[1])
    return names


def pick_profile(card, kind):
    """kind 'a2dp' | 'msbc' | 'cvsd' | 'hfp' -> profile name or None."""
    names = [p["name"] for p in (card or {}).get("profiles", []) if p["available"]]
    if kind == "a2dp":
        for pref in ("a2dp-sink-aac", "a2dp-sink", "a2dp-sink-sbc_xq", "a2dp-sink-sbc"):
            if pref in names:
                return pref
        return next((n for n in names if n.startswith("a2dp-sink")), None)
    hfp = [p for p in (card or {}).get("profiles", [])
           if p["available"] and p["name"].startswith("headset-head-unit")]
    if kind in ("msbc", "cvsd"):
        # PipeWire names vary: "headset-head-unit" (mSBC) + "-cvsd", or "-msbc" + plain (CVSD).
        # The description says the codec ("... codec mSBC)"), so trust that first.
        by_desc = next((p["name"] for p in hfp if kind in p["desc"].lower()), None)
        if by_desc:
            return by_desc
        by_name = next((p["name"] for p in hfp if kind in p["name"]), None)
        if by_name:
            return by_name
        other = "cvsd" if kind == "msbc" else "msbc"
        plain = next((p["name"] for p in hfp if p["name"] == "headset-head-unit"), None)
        if plain and any(other in p["name"] for p in hfp):
            return plain          # the plain one is the codec the suffixed one isn't
        return plain if kind == "cvsd" else None
    if not hfp:
        return None
    return (pick_profile(card, "msbc") or pick_profile(card, "cvsd") or hfp[0]["name"])


def set_profile(mac, profile, want_source=False, timeout=6.0):
    """Switch the card's profile; return seconds until the new mode's nodes exist, or None.
    Headset mode is ready when the mic source exists; music mode when the headset source is
    gone again (otherwise the old headset sink, still listed for a moment, counts as ready)."""
    card = f"bluez_card.{mac_us(mac)}"
    t0 = time.time()
    r = run(["pactl", "set-card-profile", card, profile])
    if r.returncode != 0:
        return None
    while time.time() - t0 < timeout:
        sinks = node_names("sinks", mac)
        srcs = [s for s in node_names("sources", mac) if "input" in s]
        if sinks and (bool(srcs) == want_source) and (find_card(mac) or {}).get("active") == profile:
            return round(time.time() - t0, 2)
        time.sleep(0.05)
    return None


def play(path, sink=None, timeout=30):
    cmd = ["paplay"] + ([f"--device={sink}"] if sink else []) + [path]
    return run(cmd, timeout=timeout).returncode == 0


def keep_playing(sink):
    """Endless silent stream on the music sink, so the AirPods treat the Pi as 'playing' and
    send stem presses (AVRCP). Stop it with .terminate() before switching to headset mode."""
    if not sink:
        return None
    try:
        zero = open("/dev/zero", "rb")
    except OSError:
        return None
    try:
        return subprocess.Popen(["pacat", "--playback", f"--device={sink}", "--raw", "--format=s16le",
                                 "--rate=16000", "--channels=1"],
                                stdin=zero, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    finally:
        zero.close()       # the child has its own copy


def stop_playing(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


WARMUP_S = 1.0     # music mode drops the first ~0.5-1 s after its stream starts (heard as bits and pieces)


def play_warm(paths, sink, already_warm=False):
    """Play files on the music sink with a silent stream running underneath, started WARMUP_S
    before the first file, so nothing is clipped while the AirPods' music link wakes up."""
    keep = None if already_warm else keep_playing(sink)
    try:
        if keep:
            time.sleep(WARMUP_S)
        for p in paths:
            play(p, sink)
        time.sleep(0.3)        # let the tail drain before the stream stops
    finally:
        stop_playing(keep)


class Recorder:
    """parec raw s16le mono 16 kHz from a source, read in a thread."""

    def __init__(self, source):
        self.source = source
        self.proc = None
        self.buf = bytearray()
        self.first_data = None
        self.first_sound = None
        self.started = None
        self._t = None

    def start(self):
        self.started = time.time()
        self.proc = subprocess.Popen(
            ["parec", f"--device={self.source}", "--rate=16000", "--channels=1",
             "--format=s16le", "--raw", "--latency-msec=20"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self):
        while True:
            chunk = self.proc.stdout.read(640)   # 20 ms
            if not chunk:
                return
            now = time.time()
            if self.first_data is None:
                self.first_data = now
            if self.first_sound is None and any(chunk):
                self.first_sound = now
            self.buf += chunk

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._t:
            self._t.join(timeout=2)
        return bytes(self.buf)


def record_seconds(source, seconds):
    rec = Recorder(source)
    rec.start()
    time.sleep(seconds)
    pcm = rec.stop()
    lat = {"first_data_s": round(rec.first_data - rec.started, 2) if rec.first_data else None,
           "first_sound_s": round(rec.first_sound - rec.started, 2) if rec.first_sound else None}
    return pcm, lat


def record_until_silence(source, max_s=8.0, silence_s=0.9, start_timeout=5.0):
    """Simple energy VAD: wait for speech, stop after silence_s of quiet."""
    rec = Recorder(source)
    rec.start()
    t0 = time.time()
    noise, spoke_at, last_loud = None, None, None
    pos = 0
    while time.time() - t0 < max_s:
        time.sleep(0.05)
        data = rec.buf
        while pos + 640 <= len(data):
            r = frame_rms(bytes(data[pos:pos + 640]))
            pos += 640
            if noise is None:
                noise = max(r, 30.0)
                continue
            if r > noise * 3 and r > 300:
                last_loud = time.time()
                spoke_at = spoke_at or last_loud
            elif spoke_at is None:
                noise = 0.95 * noise + 0.05 * max(r, 30.0)
        if spoke_at is None and time.time() - t0 > start_timeout:
            break
        if spoke_at and time.time() - last_loud > silence_s:
            break
    pcm = rec.stop()
    return pcm, spoke_at is not None


# ── speech recognition / synthesis ────────────────────────────────────────────

_whisper = None


def transcribe(wav_path):
    global _whisper
    try:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        t0 = time.time()
        segs, _ = _whisper.transcribe(wav_path, beam_size=1, language="en", vad_filter=False)
        text = " ".join(s.text.strip() for s in segs).strip()
        return text, round(time.time() - t0, 2)
    except Exception as e:
        return None, str(e)[:120]


def words(s):
    s = s.lower().replace("’", "'")
    return re.findall(r"[a-z0-9']+", s)


def wer(ref, hyp):
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return round(d[len(h)] / max(1, len(r)), 2)


_piper = None


def synth(text, path):
    """Piper (same voice as PC Jarvis) if available, else espeak-ng. Returns engine name."""
    global _piper
    d = os.path.join(TTS_DIR, PIPER_VOICE)
    try:
        if _piper is None and os.path.isdir(d):
            import sherpa_onnx
            onnx = next(f for f in os.listdir(d) if f.endswith(".onnx"))
            _piper = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=os.path.join(d, onnx), tokens=os.path.join(d, "tokens.txt"),
                        data_dir=os.path.join(d, "espeak-ng-data")),
                    num_threads=4)))
        if _piper is not None:
            audio = _piper.generate(text, sid=0, speed=1.0)
            pcm = b"".join(struct.pack("<h", max(-32768, min(32767, int(s * 32767)))) for s in audio.samples)
            write_wav(path, pcm, audio.sample_rate)
            return "piper"
    except Exception as e:
        say(f"  (Piper failed: {e}; using espeak-ng)")
    run(["espeak-ng", "-w", path, text], timeout=30)
    return "espeak-ng"


# ── button watchers ───────────────────────────────────────────────────────────

class BtmonWatcher:
    """Runs `sudo btmon`, logs everything, and pulls out AT commands (HFP) and AVRCP
    passthrough presses. AT commands are found by decoding each packet's hex dump,
    so a command split across dump lines is still seen."""

    HEX_LINE = re.compile(r"^\s+((?:[0-9a-f]{2} ){1,16})")

    def __init__(self, log_path):
        self.log_path = log_path
        self.events = []          # (time, kind, detail)
        self.proc = None
        self._lock = threading.Lock()

    def start(self):
        if not shutil.which("btmon"):
            say("  btmon not found (part of the bluez package) - AT-command detection off")
            return False
        self.proc = subprocess.Popen(["sudo", "stdbuf", "-oL", "btmon"], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, errors="replace")
        threading.Thread(target=self._read, daemon=True).start()
        time.sleep(0.8)
        return self.proc.poll() is None

    def _add(self, kind, detail):
        with self._lock:
            self.events.append((time.time(), kind, detail))

    def _flush(self, pkt):
        data = bytes(pkt)
        # AT+XXX (HFP), ATA / ATD (answer / dial: some headsets' button), +BRSF / +BVRA (the Pi's replies)
        for m in re.finditer(rb"(AT\+[A-Z]+[=?]?[0-9,\"A-Za-z]*|ATA(?=\r)|ATD[0-9>+*#;]*|\+BRSF:\s*\d+|\+BVRA:\s*\d)",
                             data):
            s = m.group(1).decode(errors="replace")
            self._add("at", s)

    def _read(self):
        with open(self.log_path, "w", encoding="utf-8") as log:
            self.consume(self.proc.stdout, log)

    def consume(self, lines, log=None):
        """Parse btmon output lines (live, or sample text in the self-test)."""
        pkt = bytearray()
        for line in lines:
            if log:
                log.write(line)
            if line[:1] in ("<", ">", "@", "="):   # new packet header
                self._flush(pkt)
                pkt = bytearray()
            m = self.HEX_LINE.match(line)
            if m:
                try:
                    pkt += bytes.fromhex(m.group(1).replace(" ", ""))
                except ValueError:
                    pass
            op = re.search(r"Operation:\s*0x[0-9a-f]+\s*\(([^)]*?)\s+(Pressed|Released)\)", line, re.I)
            if op:
                self._add("avrcp", f"{op.group(1).strip().upper()} {op.group(2).lower()}")
        self._flush(pkt)

    def since(self, t):
        with self._lock:
            return [e for e in self.events if e[0] >= t]

    def stop(self):
        if self.proc and self.proc.poll() is None:
            run(["sudo", "pkill", "-INT", "-x", "btmon"], timeout=5)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def key_name(ecodes, code):
    """evdev key code -> 'KEY_PLAYPAUSE' (some codes have several names; take the first)."""
    name = ecodes.KEY.get(code, str(code))
    return name[0] if isinstance(name, (list, tuple)) else name


class EvdevWatcher:
    """Key presses from BlueZ's AVRCP input device ('<name> (AVRCP)')."""

    def __init__(self, airpods_name):
        self.name = airpods_name.lower()
        self.events = []
        self.devices = []
        self._stop = threading.Event()
        self.error = None

    def start(self):
        try:
            import evdev
        except ImportError:
            self.error = "python3-evdev not installed"
            return False
        self.evdev = evdev
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def _scan(self):
        found = []
        for p in self.evdev.list_devices():
            try:
                d = self.evdev.InputDevice(p)
            except PermissionError:
                self.error = "no permission for /dev/input (log out/in after setup, or reboot)"
                continue
            except OSError:
                continue
            n = d.name.lower()
            if "avrcp" in n or (self.name and self.name[:10] in n):
                found.append(d)
        return found

    def _loop(self):
        import select
        last_scan = 0
        while not self._stop.is_set():
            if time.time() - last_scan > 2:
                last_scan = time.time()
                known = {d.path for d in self.devices}
                for d in self._scan():
                    if d.path not in known:
                        self.devices.append(d)
            if not self.devices:
                time.sleep(0.2)
                continue
            try:
                r, _, _ = select.select(self.devices, [], [], 0.2)
            except (OSError, ValueError):
                self.devices = []
                continue
            for d in r:
                try:
                    for ev in d.read():
                        if ev.type == self.evdev.ecodes.EV_KEY and ev.value == 1:
                            self.events.append((time.time(), "key", key_name(self.evdev.ecodes, ev.code)))
                except OSError:
                    self.devices = [x for x in self.devices if x is not d]

    def since(self, t):
        return [e for e in self.events if e[0] >= t]

    def stop(self):
        self._stop.set()


class AapWatcher:
    """Stem presses over Apple's own accessory protocol (AAP), which says WHICH earbud was pressed.

    Packet layout from LibrePods' reverse engineering (docs/AAP Definitions.md and
    android/.../bluetooth/AACPManager.kt in github.com/librepods-org/librepods; documented on
    AirPods Pro 2): L2CAP PSM 0x1001; a fixed handshake; then 'stem config' (control command
    0x39) with a bit per press type (1 single, 2 double, 4 triple, 8 long) tells the AirPods to
    hand those presses to us instead of acting on them (play/pause, next, noise control). Each
    press then arrives as  04 00 04 00 19 00 [type] [bud],  type 05-08, bud 01 left / 02 right.
    The setting only lasts for this connection; stop() puts the AirPods' own actions back."""

    PSM = 0x1001
    HEADER = bytes.fromhex("04000400")
    HANDSHAKE = bytes.fromhex("00000400010002000000000000000000")
    FEATURES = HEADER + bytes.fromhex("4d00d700000000000000")          # as LibrePods sends it
    NOTIFY = HEADER + bytes.fromhex("0f00ffffffff")
    PRESS = {0x05: "single", 0x06: "double", 0x07: "triple", 0x08: "long"}
    BUD = {0x01: "left", 0x02: "right"}

    def __init__(self, mac):
        self.mac = mac
        self.events = []
        self.info = {}
        self.error = None
        self.sock = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @classmethod
    def stem_config(cls, mask):
        """Control command 0x39: which press types come to us (0x0F = all, 0x00 = AirPods decide)."""
        return cls.HEADER + bytes([0x09, 0x00, 0x39, mask, 0x00, 0x00, 0x00])

    @classmethod
    def parse(cls, pkt):
        """One AAP packet -> ('press', 'left single') / ('info', {...}) / None."""
        if len(pkt) < 6 or pkt[:4] != cls.HEADER:
            return None
        op = pkt[4]
        if op == 0x19 and len(pkt) >= 8:
            return "press", f"{cls.BUD.get(pkt[7], f'bud{pkt[7]:02x}')} {cls.PRESS.get(pkt[6], f'type{pkt[6]:02x}')}"
        if op == 0x1D:      # metadata: null-terminated strings (name, model, maker, serial, firmware...)
            strings = []
            for part in pkt[6:].split(b"\x00"):
                try:
                    s = part.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if len(s) >= 2 and s.isprintable():
                    strings.append(s)
            info = {}
            if len(strings) >= 2:
                info["model"] = strings[1]
            if len(strings) >= 5:
                info["firmware"] = strings[4]
            return "info", info
        return None

    def start(self):
        try:
            self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
            self.sock.settimeout(8)
            self.sock.connect((self.mac, self.PSM))
            for pkt in (self.HANDSHAKE, self.FEATURES, self.NOTIFY, self.stem_config(0x0F)):
                self.sock.send(pkt)
                time.sleep(0.15)
        except (OSError, AttributeError) as e:
            self.error = f"AAP connection failed: {e}"
            if self.sock:
                self.sock.close()
            self.sock = None
            return False
        self.sock.settimeout(0.5)
        threading.Thread(target=self._read, daemon=True).start()
        return True

    def _read(self):
        while not self._stop.is_set():
            try:
                pkt = self.sock.recv(1024)
            except socket.timeout:
                continue
            except OSError as e:
                if not self._stop.is_set():
                    self.error = f"AAP connection lost: {e}"
                return
            if not pkt:
                return
            got = self.parse(pkt)
            if not got:
                continue
            kind, detail = got
            if kind == "press":
                with self._lock:
                    self.events.append((time.time(), "aap", detail))
            else:
                self.info.update(detail)

    def since(self, t):
        with self._lock:
            return [e for e in self.events if e[0] >= t]

    def stop(self):
        self._stop.set()
        if self.sock:
            try:
                self.sock.send(self.stem_config(0x00))     # give the presses back to the AirPods
                time.sleep(0.2)
            except OSError:
                pass
            self.sock.close()


def summarize(events):
    """Unique readable event names, ignoring the SLC chatter."""
    # connection setup and status polling, not presses (a stray one in the hold window would
    # otherwise be taken as the hold signal)
    noise = ("AT+CIND", "AT+CMER", "AT+CHLD", "AT+BIND", "AT+BIEV", "AT+XAPL", "AT+IPHONEACCEV",
             "AT+VGS", "AT+VGM", "AT+NREC", "AT+CLIP", "AT+CCWA", "AT+CMEE", "AT+BAC", "AT+BCS",
             "AT+CSRSF", "AT+APLSIRI", "AT+APLEFM", "AT+BRSF", "AT+COPS", "AT+CNUM", "AT+CLCC",
             "AT+BIA", "AT+BTRH", "AT+CSQ", "AT+CBC")
    out = []
    for _, kind, d in events:
        if kind == "at" and (d.startswith(noise) or d.startswith("+BRSF")):
            continue
        if kind == "avrcp" and d.endswith("released"):
            continue
        label = f"{kind}:{d}"
        if label not in out:
            out.append(label)
    return out


# ── steps ─────────────────────────────────────────────────────────────────────

def step_check(res):
    head("1. CHECK - is the Pi set up for the test?")
    checks = {}

    model = ""
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip("\x00\n ")
    except Exception:
        model = platform.machine()
    checks["model"] = model
    checks["arch"] = platform.machine()
    say(f"  Board: {model} ({platform.machine()})")

    def ok(name, cond, fix=""):
        checks[name] = bool(cond)
        say(f"  [{'OK' if cond else '!!'}] {name}" + ("" if cond else f"  -> {fix}"))
        return cond

    ok("bluetooth service running", run(["systemctl", "is-active", "bluetooth"]).stdout.strip() == "active",
       "sudo systemctl enable --now bluetooth")
    show = bt("show").stdout
    ok("adapter powered", "Powered: yes" in show, "bluetoothctl power on")
    for unit in ("pipewire", "pipewire-pulse", "wireplumber"):
        ok(f"{unit} running (user)", run(["systemctl", "--user", "is-active", unit]).stdout.strip() == "active",
           f"systemctl --user restart {unit}  (or rerun setup_bench.sh)")
    info = run(["pactl", "info"]).stdout
    ok("pulse server is PipeWire", "PipeWire" in info, "remove pulseaudio, rerun setup_bench.sh")
    spa = any(os.path.isdir(p) for p in ("/usr/lib/aarch64-linux-gnu/spa-0.2/bluez5",
                                         "/usr/lib/arm-linux-gnueabihf/spa-0.2/bluez5",
                                         "/usr/lib/x86_64-linux-gnu/spa-0.2/bluez5"))
    ok("PipeWire Bluetooth plugin", spa, "sudo apt install libspa-0.2-bluetooth")
    wp = (os.path.exists(os.path.expanduser("~/.config/wireplumber/wireplumber.conf.d/51-jarvis-bluetooth.conf"))
          or os.path.exists(os.path.expanduser("~/.config/wireplumber/bluetooth.lua.d/51-jarvis-bluetooth.lua")))
    ok("WirePlumber headset config", wp, "rerun setup_bench.sh")
    sco = run(["systemctl", "is-active", "jarvis-bt-sco"]).stdout.strip() == "active"
    ok("SCO routing fix applied", sco, "sudo systemctl restart jarvis-bt-sco; journalctl -u jarvis-bt-sco")
    linger = run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"]).stdout.strip()
    ok("linger on", linger.endswith("yes"), "sudo loginctl enable-linger $USER")
    groups = run(["id", "-nG"]).stdout.split()
    ok("in 'input' group (button events)", "input" in groups, "reboot after setup (or log out and back in)")
    ok("btmon available", bool(shutil.which("btmon")), "sudo apt install bluez")
    try:
        import faster_whisper  # noqa: F401
        whisper_ok = True
    except Exception:
        whisper_ok = False
    ok("faster-whisper (accuracy scoring)", whisper_ok, "optional: rerun setup_bench.sh")
    ok("Piper voice", os.path.isdir(os.path.join(TTS_DIR, PIPER_VOICE)), "optional: espeak-ng is used instead")

    res["check"] = checks
    required = ["bluetooth service running", "adapter powered", "pipewire running (user)",
                "wireplumber running (user)", "pulse server is PipeWire", "PipeWire Bluetooth plugin"]
    good = all(checks.get(k) for k in required)
    say()
    say("  Ready." if good else "  Fix the !! lines above first (most are fixed by rerunning setup + reboot).")
    return good


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02]")
PROMPT = re.compile(r"^(?:\[[^\]]*\][#>]\s*)+")


def bt_line(raw):
    """One line of interactive bluetoothctl output: colours and the '[bluetooth]# ' prompt removed."""
    return PROMPT.sub("", ANSI.sub("", raw)).strip()


def bt_device(line):
    """'[NEW] Device MAC Name' or 'Device MAC Name' -> (MAC, name); a name change
    '[CHG] Device MAC Name: X' -> (MAC, X); anything else -> None."""
    m = re.match(r"^\[CHG\] Device ([0-9A-F:]{17}) (?:Name|Alias): (.+)$", line, re.I)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    m = re.match(r"^(?:\[NEW\] )?Device ([0-9A-F:]{17}) (.+)$", line, re.I)
    if m and not re.match(r"^[\w ]+: ", m.group(2)):        # not a property line ("RSSI: -60")
        return m.group(1).upper(), m.group(2).strip()
    return None


class BtShell:
    """bluetoothctl in a pseudo-terminal, driven like a person would: its pairing agent stays
    registered for the whole session (one-off 'bluetoothctl pair' timed out on the Pi 5)."""

    def __init__(self):
        import pty
        self.fd, slave = pty.openpty()
        self.proc = subprocess.Popen(["bluetoothctl"], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        self.lines = queue.Queue()
        self.log = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = ""
        while True:
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk.decode(errors="replace")
            *done, buf = re.split(r"\r?\n|\r", buf)
            for raw in done:
                line = bt_line(raw)
                if line:
                    self.log.append(line)
                    self.lines.put(line)

    def send(self, cmd):
        os.write(self.fd, (cmd + "\n").encode())

    def wait(self, pattern, timeout):
        """First new line matching pattern (regex), or None after timeout."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.lines.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                break
            m = re.search(pattern, line)
            if m:
                return m
        return None

    def close(self):
        try:
            self.send("quit")
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()
        try:
            os.close(self.fd)
        except OSError:
            pass


def pair_interactive(scan_s=30):
    """Scan, pair the AirPods as soon as they show up, trust them. -> {ok, mac, name} or {ok: False, why}."""
    sh = BtShell()
    try:
        for cmd in ("agent on", "default-agent", "power on"):
            sh.send(cmd)
            time.sleep(0.3)
        say("  1. Put both AirPods in the case, lid OPEN.")
        say("  2. Hold the button on the back of the case until the light flashes WHITE.")
        say("     (If they're connected to your iPhone, that's fine - pairing mode overrides it.)")
        ask("  Press Enter when the light is flashing white...")
        say(f"  Scanning (up to {scan_s} s)...")
        sh.send("scan on")
        seen, end = {}, time.time() + scan_s
        while time.time() < end:
            m = sh.wait(r".", max(0.1, end - time.time()))
            dev = bt_device(m.string) if m else None
            if dev:
                seen[dev[0]] = dev[1]
                if "airpod" in dev[1].lower():
                    break
        sh.send("scan off")
        devices = [(m_, n_) for m_, n_ in seen.items() if "airpod" in n_.lower()]
        if not devices and seen:
            # the name isn't always resolved during a scan (shows as the MAC with dashes)
            say("  Nothing named 'AirPods' showed up. Devices seen:")
            found = list(seen.items())
            for i, (m_, n_) in enumerate(found, 1):
                say(f"    {i}. {n_} ({m_})")
            pick = ask_int("  Number of the AirPods (0 = none of these)? [0]", 0, 0, len(found))
            devices = [found[pick - 1]] if pick else []
        if not devices:
            return {"ok": False, "why": "AirPods not found in the scan (was the light flashing white?)"}
        mac, name = devices[0]
        say(f"  Found {name} ({mac}). Pairing...")
        sh.send(f"pair {mac}")
        m = sh.wait(r"Pairing successful|Failed to pair: ?(\S*)|AlreadyExists", 40)
        if not m:
            return {"ok": False, "why": "no answer to 'pair' within 40 s", "output": "\n".join(sh.log[-15:])}
        if m.group(0).startswith("Failed") and "AlreadyExists" not in m.group(0):
            return {"ok": False, "why": m.group(0), "output": "\n".join(sh.log[-15:])}
        sh.send(f"trust {mac}")
        sh.wait(r"trust succeeded", 5)
        return {"ok": True, "mac": mac, "name": name}
    finally:
        sh.close()


def step_pair(res):
    head("2. PAIR - connect the AirPods to the Pi")
    cfg = res.setdefault("config", {})
    mac = cfg.get("mac")

    if mac and bt_info(mac).get("Paired") == "yes":
        say(f"  Already paired: {cfg.get('name')} ({mac})")
    else:
        found = pair_interactive()
        if not found.get("ok"):
            say(f"  Pairing failed: {found.get('why')}")
            if found.get("output"):
                say("    " + found["output"].replace("\n", "\n    "))
            say("  Pair by hand, then run this step again:")
            say("    bluetoothctl -> agent on -> default-agent -> scan on -> (white light) -> pair <MAC> -> trust <MAC>")
            res["pair"] = {"ok": False, "why": found.get("why"), "output": found.get("output", "")}
            return False
        mac, name = found["mac"], found["name"]
        cfg.update(mac=mac, name=name)

    say("  Put the AirPods in your ears. Connecting...")
    t0 = time.time()
    for _ in range(3):
        bt("connect", mac, timeout=25)
        if bt_info(mac).get("Connected") == "yes":
            break
        time.sleep(2)
    connected = bt_info(mac).get("Connected") == "yes"
    conn_s = round(time.time() - t0, 1)
    # the card appears with music mode first; the headset (HFP) profile follows once its own
    # connection is set up, often a few seconds later
    card = None
    for _ in range(48):
        card = find_card(mac)
        if card and pick_profile(card, "hfp"):
            break
        time.sleep(0.25)

    profiles = [p["name"] for p in (card or {}).get("profiles", []) if p["available"]]
    say(f"  Connected: {'yes' if connected else 'NO'} ({conn_s}s)")
    say(f"  Audio profiles PipeWire offers: {', '.join(profiles) if profiles else 'NONE'}")
    has_a2dp = bool(pick_profile(card, "a2dp"))
    has_hfp = bool(pick_profile(card, "hfp"))
    if connected and not has_hfp:
        say("  !! No headset (HFP) profile - the mic can't work until this shows up.")
        say("     Usually: WirePlumber config not loaded. Rerun setup_bench.sh, reboot, then")
        say("     'bluetoothctl disconnect <MAC>' and run this step again.")
    res["pair"] = {"ok": connected and has_a2dp, "connected": connected, "connect_s": conn_s,
                   "profiles": profiles, "a2dp": has_a2dp, "hfp": has_hfp,
                   "msbc": bool(pick_profile(card, "msbc")),
                   "active_profile": (card or {}).get("active")}
    return res["pair"]["ok"]


def need_mac(res):
    mac = res.get("config", {}).get("mac")
    if not mac:
        say("  Run './scripts/pocket/bench pair' first.")
        return None
    if bt_info(mac).get("Connected") != "yes":
        say("  AirPods not connected - connecting (put them in your ears)...")
        bt("connect", mac, timeout=25)
        time.sleep(2)
        if bt_info(mac).get("Connected") != "yes":
            say("  Couldn't connect. Take them out of the case, then: bluetoothctl connect " + mac)
            return None
    return mac


def to_a2dp(mac):
    card = find_card(mac)
    prof = pick_profile(card, "a2dp")
    if not prof:
        return None, None
    secs = set_profile(mac, prof)
    sinks = node_names("sinks", mac)
    return (sinks[0] if sinks else None), secs


def step_sound(res):
    head("3. SOUND - music mode (A2DP): can you hear the Pi?")
    mac = need_mac(res)
    if not mac:
        res["sound"] = {"ok": False, "why": "not connected"}
        return False
    sink, secs = to_a2dp(mac)
    if not sink:
        say("  No music-mode output appeared.")
        res["sound"] = {"ok": False, "why": "no a2dp sink"}
        return False
    run(["pactl", "set-default-sink", sink])
    run(["pactl", "set-sink-volume", sink, "70%"])
    os.makedirs(RESULTS_DIR, exist_ok=True)
    chime = tone_wav(os.path.join(RESULTS_DIR, "chime.wav"), [(880, 0.12), (0, 0.05), (1320, 0.18)])
    voice = os.path.join(RESULTS_DIR, "hello.wav")
    engine = synth("Hello sir. If you can hear me, the Pi is talking to your AirPods.", voice)
    say(f"  Playing a chime, then a sentence ({engine}) on {sink} ...")
    play_warm([chime, voice], sink)
    heard = yes("  Did you hear both clearly in the AirPods?")
    res["sound"] = {"ok": heard, "sink": sink, "switch_s": secs, "tts_engine": engine}
    return heard


GESTURES = [
    ("left_single", "LEFT earbud: SINGLE-PRESS the stem"),
    ("left_double", "LEFT earbud: DOUBLE-PRESS the stem"),
    ("left_triple", "LEFT earbud: TRIPLE-PRESS the stem"),
    ("left_hold", "LEFT earbud: PRESS AND HOLD the stem (about 2 seconds), then let go"),
    ("right_single", "RIGHT earbud: SINGLE-PRESS the stem"),
    ("right_double", "RIGHT earbud: DOUBLE-PRESS the stem"),
    ("right_hold", "RIGHT earbud: PRESS AND HOLD the stem (about 2 seconds), then let go"),
]

# what the right earbud would do in the pocket client, once the left one is Jarvis's
RIGHT_MUSIC = {"single": "play/pause", "double": "next track", "triple": "previous track", "long": "noise control"}


def pick_trigger(results):
    """results: {gesture: [summarized events]} -> {trigger, match, ...}, best first:
    'left'   - AAP says which earbud: any press on the left = Jarvis, right stays for music
    'hold'   - press-and-hold sent AT+BVRA (HFP voice recognition)
    'double' - double-press sent next-track (AVRCP) while single didn't
    'press'  - every press looks the same (e.g. all PLAY): any press = Jarvis
    'none'   - nothing arrived."""
    left = [e for k, v in results.items() if k.startswith("left") for e in v if e.startswith("aap:left ")]
    right = [e for k, v in results.items() if k.startswith("right") for e in v if e.startswith("aap:right ")]
    hold = [e for k, v in results.items() if k.endswith("hold") for e in v]
    if left and right:
        return {"trigger": "left", "match": ["aap:left "],
                "left_long": any(e.endswith(" long") for e in left),
                "right_presses": sorted({e.split()[-1] for e in right})}
    bvra = [e for e in hold if "BVRA" in e]
    if bvra:
        return {"trigger": "hold", "match": bvra[:1]}
    single = results.get("left_single", []) + results.get("right_single", [])
    double = results.get("left_double", []) + results.get("right_double", [])
    nxt = [e for e in double if re.search(r"NEXT|FORWARD", e) and e not in single]
    if nxt:
        return {"trigger": "double", "match": nxt[:1]}
    every = sorted({e for v in results.values() for e in v})
    if every:
        # an AAP press without a usable left/right split still works as 'any press'
        return {"trigger": "press", "match": sorted({e if not e.startswith("aap:") else "aap:" for e in every})}
    return {"trigger": "none", "match": []}


def trigger_hit(match, events):
    """Did any event (summarized labels) match the trigger? Entries ending in ' ' or ':' are prefixes."""
    return next((e for e in events for m in match if (e.startswith(m) if m.endswith((" ", ":")) else e == m)),
                None)


def step_buttons(res):
    head("4. BUTTONS - which stem presses reach the Pi?")
    mac = need_mac(res)
    if not mac:
        res["buttons"] = {"ok": False, "why": "not connected"}
        return False
    name = res.get("config", {}).get("name", "AirPods")
    say("  Keep the AirPods away from the iPhone (or turn its Bluetooth off) so it doesn't")
    say("  grab them. Each gesture is done on the earbud named, LEFT or RIGHT.")
    ask("  Press Enter when ready (sudo may ask for your password)...")
    run(["sudo", "-v"], timeout=60)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    btmon = BtmonWatcher(os.path.join(RESULTS_DIR, "btmon_buttons.log"))
    evw = EvdevWatcher(name)
    have_btmon = btmon.start()
    evw.start()

    # Reconnect while btmon listens, to capture the HFP handshake (what features the Pi offers)
    say("  Reconnecting once so the handshake is captured...")
    bt("disconnect", mac, timeout=15)
    time.sleep(2)
    bt("connect", mac, timeout=25)
    time.sleep(4)
    handshake = [d for _, k, d in btmon.since(0) if k == "at"]
    ag_feat = None
    for d in handshake:
        m = re.match(r"\+BRSF:\s*(\d+)", d)
        if m:
            ag_feat = int(m.group(1))
    voice_rec_offered = None if ag_feat is None else bool(ag_feat & 0x4)

    # Apple's protocol, for left/right and press-and-hold (connect after the reconnect above)
    aap = AapWatcher(mac)
    if aap.start():
        say("  Connected over Apple's accessory protocol (AAP): presses should say which earbud.")
    else:
        say(f"  ({aap.error} - falling back to standard Bluetooth presses only)")

    # keep music mode active with a silent stream so the AirPods treat it as "playing"
    sink, _ = to_a2dp(mac)
    keep = keep_playing(sink)

    results = {}
    try:
        for key, text in GESTURES:
            say()
            say(f"  -> {text}.")
            ask("     Press Enter, then do it within 6 seconds...")
            t0 = time.time()
            for i in range(6, 0, -1):
                sys.stdout.write(f"\r     listening... {i} ")
                sys.stdout.flush()
                time.sleep(1)
            sys.stdout.write("\r" + " " * 30 + "\r")
            got = summarize(btmon.since(t0) + evw.since(t0) + aap.since(t0))
            results[key] = got
            say(f"     seen: {', '.join(got) if got else 'nothing'}")
    finally:
        stop_playing(keep)
        aap.stop()
        btmon.stop()
        evw.stop()

    pick = pick_trigger(results)
    trigger = pick["trigger"]
    res["buttons"] = {
        "ok": trigger != "none",
        **pick,
        "seen": results,
        "aap": aap.error is None,
        "aap_error": aap.error,
        "aap_info": aap.info,
        "btmon": have_btmon,
        "evdev_devices": [d.name for d in evw.devices],
        "evdev_error": evw.error,
        "ag_features": ag_feat,
        "ag_offers_voice_recognition": voice_rec_offered,
        "handshake_at": handshake[:40],
    }
    say()
    if trigger == "left":
        say("  Left and right are told apart: any LEFT press can be Jarvis, the RIGHT stays for music"
            f" ({', '.join(pick['right_presses'])} arrived from the right).")
        say("  Press-and-hold on the left arrives too." if pick["left_long"] else
            "  (Press-and-hold on the left didn't arrive; other left presses do.)")
    elif trigger == "hold":
        say(f"  Press-and-hold reaches the Pi ({pick['match'][0]}).")
    elif trigger == "double":
        say(f"  Double-press is distinct ({pick['match'][0]}): usable as the trigger.")
    elif trigger == "press":
        say(f"  Presses arrive but all look alike ({', '.join(pick['match'])}): any press = Jarvis.")
    else:
        say("  No usable button event arrived.")
    return trigger != "none"


def mic_once(mac, kind, seconds=6):
    """Switch to headset mode with the given codec, record the test sentence, measure it."""
    card = find_card(mac)
    prof = pick_profile(card, kind)
    if not prof:
        return {"codec": kind, "ok": False, "why": "profile not offered"}
    t_sw = set_profile(mac, prof, want_source=True)
    if t_sw is None:
        return {"codec": kind, "profile": prof, "ok": False, "why": "headset mode didn't start"}
    srcs = [s for s in node_names("sources", mac) if "input" in s]
    sinks = node_names("sinks", mac)
    if not srcs:
        return {"codec": kind, "profile": prof, "ok": False, "why": "no mic source appeared"}
    src = srcs[0]
    run(["pactl", "set-source-volume", src, "100%"])

    chime = os.path.join(RESULTS_DIR, "chime.wav")
    if not os.path.exists(chime):
        tone_wav(chime, [(880, 0.12), (0, 0.05), (1320, 0.18)])
    say(f"     After the chime, say:  \"{TEST_SENTENCE}\"")
    time.sleep(0.6)
    if sinks:
        play(chime, sinks[0])
    pcm, lat = record_seconds(src, seconds)
    path = os.path.join(RESULTS_DIR, f"mic_{kind}.wav")
    write_wav(path, pcm)
    st = pcm_stats(pcm)
    silent = st["peak"] == 0 or st["zero_frac"] > 0.98
    out = {"codec": kind, "profile": prof, "switch_s": t_sw, **st, **lat,
           "file": os.path.relpath(path, REPO), "silent": silent}
    if silent:
        out.update(ok=False, why="recording is silence (SCO audio not reaching Linux)")
        return out
    text, t_asr = transcribe(path)
    if text is not None:
        out.update(transcript=text, wer=wer(TEST_SENTENCE, text), asr_s=t_asr)
    out["ok"] = True
    return out


def best_mic(runs):
    """The usable recording with the lowest word error (then best rating), or None. Usable = word
    error <= 35% or rated 3+/5; a run with no transcript (no Whisper) is judged on its rating alone."""
    good = [o for o in runs if o.get("ok") and (o.get("wer", 1) <= 0.35 or o.get("rating", 0) >= 3)]
    return min(good, key=lambda o: (o.get("wer", 1), -o.get("rating", 0))) if good else None


def step_mic(res):
    head("5. MIC - headset mode (HFP): does the AirPods mic work?")
    mac = need_mac(res)
    if not mac:
        res["mic"] = {"ok": False, "why": "not connected"}
        return False
    os.makedirs(RESULTS_DIR, exist_ok=True)
    say("  Re-applying the SCO routing fix (sudo)...")
    r = run(["sudo", sys.executable if os.path.exists(sys.executable) else "python3",
             os.path.join(HERE, "sco_route.py")], timeout=30)
    sco_msg = (r.stdout + r.stderr).strip()
    say(f"     {sco_msg[-160:]}")

    runs = []
    for kind in ("msbc", "cvsd"):
        say()
        say(f"  -- {'wideband (mSBC, 16 kHz)' if kind == 'msbc' else 'narrowband (CVSD, 8 kHz)'} --")
        out = mic_once(mac, kind)
        if out.get("why") == "profile not offered":
            say("     not offered by PipeWire/AirPods, skipping")
            runs.append(out)
            continue
        if not out["ok"]:
            say(f"     FAILED: {out['why']}")
        else:
            say(f"     level {out['rms_dbfs']} dBFS, mode switch {out['switch_s']}s, "
                f"first sound after {out.get('first_sound_s')}s")
            if "transcript" in out:
                say(f"     heard: \"{out['transcript']}\"  (word error {int(out['wer'] * 100)}%, {out['asr_s']}s)")
        runs.append(out)

    # play each good recording back in music mode and get a rating
    sink, back_s = to_a2dp(mac)
    for out in runs:
        if out.get("ok") and sink:
            say()
            say(f"  Playing back the {out['codec']} recording...")
            time.sleep(0.4)
            play_warm([os.path.join(REPO, out["file"])], sink)
            out["rating"] = ask_int("     How clear was your voice, 1 (unusable) to 5 (like a phone call)? [3]",
                                    3, 1, 5)

    best = best_mic(runs)
    res["mic"] = {"ok": bool(best), "best": best and best["codec"], "runs": runs,
                  "back_to_music_s": back_s, "sco_fix": sco_msg[-200:]}
    say()
    if best:
        say(f"  The AirPods mic works over {best['codec'].upper()}.")
    else:
        silent = any(o.get("silent") for o in runs)
        say("  The mic test failed." + (" Recordings were silent: the SCO audio isn't reaching Linux."
                                        if silent else ""))
    return bool(best)


_enter_q = None


def _enter_queue():
    """One background reader for Enter presses (started once; input() isn't used after it)."""
    global _enter_q
    if _enter_q is None:
        _enter_q = queue.Queue()

        def reader():
            for _ in iter(sys.stdin.readline, ""):
                _enter_q.put(True)

        threading.Thread(target=reader, daemon=True).start()
    return _enter_q


TRIGGER_NAMES = {"left": "any press on the LEFT earbud", "hold": "press-and-hold", "double": "double-press",
                 "press": "any stem press", "none": "the Enter key"}


def wait_trigger(res, watchers, sink=None, timeout=None):
    """Block until the chosen trigger happens. Returns how it was triggered ('enter' or the event).
    A silent stream plays on the music sink meanwhile, as in the buttons step (stem presses need
    'playing'). Right-earbud presses are shown as the music control they'd become."""
    keep = keep_playing(sink)
    try:
        return _wait_trigger(res, watchers, timeout)
    finally:
        stop_playing(keep)


def _wait_trigger(res, watchers, timeout):
    b = res.get("buttons", {})
    trig = b.get("trigger", "none")
    match = b.get("match", [])
    say(f"  Waiting for {TRIGGER_NAMES.get(trig, trig)}{', or press Enter' if trig != 'none' else ''}"
        f" (Ctrl+C to finish)...")
    enter = _enter_queue()
    while not enter.empty():          # ignore Enter presses from before
        enter.get_nowait()
    t0, shown = time.time(), 0
    while timeout is None or time.time() - t0 < timeout:
        if not enter.empty():
            return "enter"
        raw = [e for w in watchers for e in w.since(t0)]
        hit = trigger_hit(match, summarize(raw)) if match else None
        if hit:
            return hit
        right = [d for _, k, d in sorted(raw) if k == "aap" and d.startswith("right ")]
        for d in right[shown:]:
            say(f"  (right {d.split()[-1]} press -> music: {RIGHT_MUSIC.get(d.split()[-1], '?')})")
        shown = len(right)
        time.sleep(0.05)
    return None


def step_jarvis(res, url=None):
    head("6. JARVIS - press, speak, hear the answer (talks to the PC Jarvis)")
    try:
        import requests
    except ImportError:
        say("  python3-requests missing; rerun setup_bench.sh")
        return False
    cfg = res.setdefault("config", {})
    url = (url or cfg.get("jarvis_url") or "").rstrip("/")
    if not url:
        say("  The PC Jarvis address is the phone link it prints at startup, without /phone,")
        say("  e.g. https://your-pc.tail1234.ts.net:5151  (Tailscale must be on for both).")
        url = ask("  PC Jarvis address:").rstrip("/")
        if url.endswith("/phone"):
            url = url[:-6]
    if not url:
        return False
    try:
        r = requests.get(url + "/status", timeout=6)
        r.raise_for_status()
        cfg["jarvis_url"] = url
        say(f"  Reached Jarvis at {url}")
    except Exception as e:
        say(f"  Can't reach {url}/status: {e}")
        say("  Check: Jarvis running on the PC, Tailscale up on both ('tailscale status'), the address.")
        res["jarvis"] = {"ok": False, "why": f"unreachable: {e}"[:200]}
        return False

    mac = need_mac(res)
    if not mac:
        return False
    mic_kind = res.get("mic", {}).get("best") or "msbc"
    card = find_card(mac)
    hfp_prof = pick_profile(card, mic_kind) or pick_profile(card, "hfp")
    if not hfp_prof:
        say("  No headset profile; run the mic step first.")
        return False

    run(["sudo", "-v"], timeout=60)
    btmon = BtmonWatcher(os.path.join(RESULTS_DIR, "btmon_jarvis.log"))
    evw = EvdevWatcher(cfg.get("name", "AirPods"))
    btmon.start()
    evw.start()
    aap = AapWatcher(mac)
    if res.get("buttons", {}).get("trigger") in ("left", "press") and not aap.start():
        say(f"  ({aap.error}; Enter still works)")
    chime = tone_wav(os.path.join(RESULTS_DIR, "chime.wav"), [(880, 0.12), (0, 0.05), (1320, 0.18)])
    done_tone = tone_wav(os.path.join(RESULTS_DIR, "done.wav"), [(1320, 0.1), (0, 0.04), (880, 0.14)])
    sink, _ = to_a2dp(mac)

    turns = []
    warm = None
    say("  Try a few real commands (\"what time is it\", \"what's on my agenda today\").")
    try:
        while True:
            stop_playing(warm)
            warm = None
            how = wait_trigger(res, [btmon, evw, aap], sink)
            if not how:
                break
            t = {"trigger": how}
            t_press = time.time()
            sw = set_profile(mac, hfp_prof, want_source=True)
            srcs = [s for s in node_names("sources", mac) if "input" in s]
            sinks = node_names("sinks", mac)
            if sw is None or not srcs:
                say("  Headset mode didn't start; try again.")
                sink, _ = to_a2dp(mac)
                continue
            if sinks:
                play(chime, sinks[0])
            t["press_to_listening_s"] = round(time.time() - t_press, 2)
            say("  Listening...")
            pcm, spoke = record_until_silence(srcs[0])
            t_end = time.time()
            sink, back = to_a2dp(mac)
            t["back_to_music_s"] = back
            warm = keep_playing(sink)      # music link warmed up while Jarvis thinks: no clipped reply
            if not spoke:
                say("  Didn't hear anything.")
                turns.append({**t, "ok": False, "why": "no speech"})
                continue
            wav_path = os.path.join(RESULTS_DIR, "last_command.wav")
            write_wav(wav_path, pcm)

            try:
                before = requests.get(url + "/status", timeout=6).json().get("log", [])
            except Exception:
                before = []
            try:
                with open(wav_path, "rb") as f:
                    r = requests.post(url + "/phone-command", files={"audio": ("command.wav", f, "audio/wav")},
                                      timeout=40)
                body = r.json()
            except Exception as e:
                say(f"  Sending failed: {e}")
                turns.append({**t, "ok": False, "why": f"post failed: {e}"[:120]})
                continue
            if r.status_code != 200:
                say(f"  Jarvis: {body.get('error')}")
                turns.append({**t, "ok": False, "why": body.get("error")})
                continue
            t["heard"] = body.get("command")
            t["upload_and_stt_s"] = round(time.time() - t_end, 2)
            say(f"  Jarvis heard: \"{t['heard']}\"")

            replies, first_reply_at, last_new = [], None, None
            deadline = time.time() + 45
            while time.time() < deadline:
                time.sleep(0.5)
                try:
                    log = requests.get(url + "/status", timeout=6).json().get("log", [])
                except Exception:
                    continue
                new = new_entries(before, log)
                got = [e["text"] for e in new if e.get("role") == "jarvis"]
                if len(got) > len(replies):
                    replies = got
                    last_new = time.time()
                    first_reply_at = first_reply_at or last_new
                if replies and time.time() - last_new > 1.5:
                    break
            if not replies:
                say("  No reply within 45 s.")
                turns.append({**t, "ok": False, "why": "no reply"})
                continue
            text = " ".join(replies)
            t["reply"] = text
            t["speech_end_to_reply_text_s"] = round(first_reply_at - t_end, 2)
            out = os.path.join(RESULTS_DIR, "reply.wav")
            t0 = time.time()
            synth(text, out)
            t["tts_s"] = round(time.time() - t0, 2)
            t["speech_end_to_voice_s"] = round(time.time() - t_end, 2)
            say(f"  Jarvis: {text}")
            play_warm([out, done_tone], sink, already_warm=warm is not None)
            t["ok"] = True
            turns.append(t)
            say(f"  (end of speech -> reply in your ears: {t['speech_end_to_voice_s']}s)")
    except KeyboardInterrupt:
        say()
    finally:
        stop_playing(warm)
        aap.stop()
        btmon.stop()
        evw.stop()

    good = [x for x in turns if x.get("ok")]
    avg = lambda k: round(sum(x[k] for x in good) / len(good), 2) if good else None  # noqa: E731
    res["jarvis"] = {"ok": bool(good), "turns": turns, "successful": len(good),
                     "avg_press_to_listening_s": avg("press_to_listening_s"),
                     "avg_speech_end_to_voice_s": avg("speech_end_to_voice_s")}
    say(f"  {len(good)} of {len(turns)} round trips worked.")
    return bool(good)


def new_entries(before, log):
    """Entries in `log` (the /status conversation log) that weren't in `before`. Compared as a set
    rather than by the last entry: a corrected transcript changes an old entry's text, and the
    log keeps only the last 40, either of which made 'after the last seen one' return old replies."""
    seen = {(e.get("role"), e.get("time"), e.get("sample") or e.get("text")) for e in before}
    return [e for e in log if (e.get("role"), e.get("time"), e.get("sample") or e.get("text")) not in seen]


# ── report ────────────────────────────────────────────────────────────────────

def verdict(res):
    mic = res.get("mic", {})
    btn = res.get("buttons", {})
    snd = res.get("sound", {})
    if not res.get("pair", {}).get("ok") or not snd.get("ok"):
        return "INCOMPLETE", "Pairing or music playback didn't work yet - fix that before judging the idea."
    if not mic:
        return "INCOMPLETE", "Mic step not run yet."
    if not mic.get("ok"):
        return ("NO-GO (AirPods mic)",
                "Music works but the AirPods mic doesn't reach Linux. Before redirecting: send this report to "
                "Claude - silent recordings usually mean SCO routing, which may be fixable. If it can't be fixed, "
                "the fallback is a small mic on the device (or a BLE remote + phone mic), keeping AirPods for listening.")
    trig = btn.get("trigger")
    if trig == "left":
        return ("GO (left earbud = Jarvis)",
                "AirPods mic works, and Apple's accessory protocol tells the earbuds apart: any press on the left "
                "starts Jarvis, the right stays free for music controls"
                + (", press-and-hold included." if btn.get("left_long") else " (press-and-hold on the left didn't arrive)."))
    if trig == "hold":
        return "GO", "AirPods mic works and press-and-hold reaches the Pi. The plan stands as written."
    if trig == "double":
        return ("GO (double-press trigger)",
                "AirPods mic works; press-and-hold didn't arrive but double-press does. Use double-press "
                "(\"next song\" then becomes a voice command).")
    if trig == "press":
        return ("GO (any-press trigger)",
                "AirPods mic works; stem presses arrive but all look the same, so any press starts Jarvis "
                "and music controls go through voice.")
    if not btn:
        return "LIKELY GO", "Mic works; run the buttons step to choose the trigger."
    return ("GO (needs another trigger)",
            "Mic works but no stem press reached the Pi. Options: a tiny button on the device, "
            "a $10 BLE remote, or a wake word on the device (costs battery).")


def write_report(res):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    v, why = verdict(res)
    chk = res.get("check", {})
    pr = res.get("pair", {})
    btn = res.get("buttons", {})
    mic = res.get("mic", {})
    jv = res.get("jarvis", {})
    L = [f"# AirPods bench report - {res.get('updated', '')}", "",
         f"**Verdict: {v}**  ", why, "",
         f"Board: {chk.get('model', '?')} · AirPods: {res.get('config', {}).get('name', '?')}", "",
         "| Test | Result |", "|---|---|"]
    L.append(f"| Pairing | {'OK' if pr.get('ok') else 'FAILED' if pr else 'not run'}"
             + (f" ({pr.get('connect_s')} s); profiles: {', '.join(pr.get('profiles', []))}" if pr else "") + " |")
    s = res.get("sound", {})
    L.append(f"| Music mode (A2DP) | {'heard' if s.get('ok') else 'FAILED' if s else 'not run'} |")
    if btn:
        seen = btn.get("seen", {})
        for key, got in seen.items():
            L.append(f"| Press: {key.replace('_', ' ')} | {', '.join(got) or 'nothing'} |")
        L.append(f"| Trigger to use | **{btn.get('trigger')}** ({TRIGGER_NAMES.get(btn.get('trigger'), '?')}) |")
        aap_txt = ("connected" if btn.get("aap") else f"not available ({btn.get('aap_error')})")
        info = btn.get("aap_info") or {}
        if info:
            aap_txt += " · " + ", ".join(f"{k} {v}" for k, v in info.items())
        L.append(f"| Apple protocol (AAP) | {aap_txt} |")
        if btn.get("ag_features") is not None:
            L.append(f"| Pi HFP features (+BRSF) | {btn['ag_features']} (voice recognition offered: "
                     f"{btn.get('ag_offers_voice_recognition')}) |")
    else:
        L.append("| Buttons | not run |")
    for run_ in mic.get("runs", []):
        if run_.get("why") == "profile not offered":
            L.append(f"| Mic {run_['codec'].upper()} | not offered |")
            continue
        if run_.get("ok"):
            txt = (f"OK · level {run_['rms_dbfs']} dBFS · switch {run_['switch_s']} s · "
                   f"first sound {run_.get('first_sound_s')} s · rating {run_.get('rating', '?')}/5")
            if "transcript" in run_:
                txt += f" · word error {int(run_['wer'] * 100)}% (\"{run_['transcript']}\")"
        else:
            txt = f"FAILED: {run_.get('why')}"
        L.append(f"| Mic {run_['codec'].upper()} | {txt} |")
    if not mic:
        L.append("| Mic | not run |")
    if mic.get("back_to_music_s") is not None:
        L.append(f"| Headset -> music switch | {mic['back_to_music_s']} s |")
    if jv:
        L.append(f"| Jarvis round trips | {jv.get('successful', 0)} of {len(jv.get('turns', []))} "
                 f"· press->listening {jv.get('avg_press_to_listening_s')} s "
                 f"· end of speech->voice {jv.get('avg_speech_end_to_voice_s')} s |")
    else:
        L.append("| Jarvis round trip | not run |")
    L += ["", "Caveat: the Pi 5 and Zero 2 W use different Bluetooth chips (CYW43455 vs CYW43438). "
          "A pass here is a strong sign for the Zero, not a guarantee; rerun this bench on the Zero when it arrives.",
          "", "<details><summary>Raw results</summary>", "", "```json",
          json.dumps({k: v for k, v in res.items() if k != "config"}, indent=1)[:6000], "```", "</details>"]
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return v, why


def step_report(res):
    v, why = write_report(res)
    head("REPORT")
    with open(REPORT_FILE, encoding="utf-8") as f:
        text = f.read()
    say(text.split("<details>")[0])
    say(f"Full report: {os.path.relpath(REPORT_FILE, REPO)}  (paste it to Claude)")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="AirPods bench test for the Jarvis pocket device")
    ap.add_argument("step", choices=["check", "pair", "sound", "buttons", "mic", "jarvis", "all", "report", "reset",
                                     "selftest"])
    ap.add_argument("--jarvis", help="PC Jarvis address, e.g. https://my-pc.tail1234.ts.net:5151")
    args = ap.parse_args()

    if args.step == "selftest":            # offline, no Bluetooth, runs anywhere
        from bench_selftest import selftest
        sys.exit(0 if selftest() else 1)
    if os.geteuid() == 0:
        say("Run as your normal user (no sudo) - audio lives in your user session.")
        sys.exit(1)
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    res = load_results()

    try:
        if args.step == "reset":
            keep = {"config": res.get("config", {})}
            save_results(keep)
            say("Results cleared (pairing kept).")
            return
        if args.step == "report":
            step_report(res)
            return
        steps = {
            "check": step_check, "pair": step_pair, "sound": step_sound,
            "buttons": step_buttons, "mic": step_mic,
            "jarvis": lambda r: step_jarvis(r, args.jarvis),
        }
        if args.step == "all":
            for name in ("check", "pair", "sound", "buttons", "mic"):
                ok = steps[name](res)
                save_results(res)
                if not ok and name in ("check", "pair"):
                    say("\nStopping here - fix this step, then run './scripts/pocket/bench all' again.")
                    break
            else:
                if res.get("mic", {}).get("ok") and yes("\nTry real commands with the PC Jarvis now?"):
                    steps["jarvis"](res)
        else:
            steps[args.step](res)
    except KeyboardInterrupt:
        say("\nStopped.")
    finally:
        save_results(res)
        if args.step not in ("report", "reset"):
            v, _ = write_report(res)
            say(f"\nVerdict so far: {v}.  './scripts/pocket/bench report' prints the summary.")


if __name__ == "__main__":
    main()
