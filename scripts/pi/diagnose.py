#!/usr/bin/env python3
"""
Health checks for the Pi 5 running Jarvis at home, unattended. Standard library only.

    ./scripts/pi/diag check              hardware snapshot: heat, fan, power supply, memory, storage, services
    ./scripts/pi/diag sd                 the SD card: speed, small-write latency (what Jarvis does), errors
    ./scripts/pi/diag stress [--minutes 15]   all 4 cores flat out: how hot does it get, does it slow down?
    ./scripts/pi/diag load [--rounds 10]      real spoken commands to the running Jarvis: latency, heat, memory
    ./scripts/pi/diag soak [--hours 24]       log everything every minute (run it inside tmux), then summarize
    ./scripts/pi/diag report             all results in one report.md (paste it to Claude)
    ./scripts/pi/diag selftest           offline check of the parsers and summaries (any OS)

Results go to scripts/pi/results/ (git-ignored).
"""

import argparse
import csv
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RESULTS = os.path.join(HERE, "results")
sys.path.insert(0, HERE)
import pihealth  # noqa: E402

LOAD_COMMANDS = ["What time is it?", "What's the date today?", "What's on my agenda today?",
                 "Tell me a fun fact.", "How many days until Christmas?", "Give me a breakdown of today."]

# SD Association speed classes (minimum sustained values)
A1 = {"read_iops": 1500, "write_iops": 500}
A2 = {"read_iops": 4000, "write_iops": 2000}


def say(msg=""):
    print(msg, flush=True)


def head(title):
    say()
    say("=" * 64)
    say(f"  {title}")
    say("=" * 64)


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not installed")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def save(name, data):
    os.makedirs(RESULTS, exist_ok=True)
    data["when"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(RESULTS, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def load(name):
    try:
        with open(os.path.join(RESULTS, f"{name}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read(path):
    try:
        with open(path) as f:
            return f.read().strip("\x00\n ")
    except OSError:
        return None


def item(results, name, level, detail):
    """level: ok / warn / bad / info"""
    results.append({"name": name, "level": level, "detail": detail})
    mark = {"ok": "OK", "warn": "!!", "bad": "XX", "info": "--"}[level]
    say(f"  [{mark}] {name}: {detail}")


# ── check ─────────────────────────────────────────────────────────────────────

def card_info(disk):
    """SD card identity from the kernel (no root needed)."""
    if not disk or not disk.startswith("mmcblk"):
        return None
    d = f"/sys/block/{disk}/device"
    size = read(f"/sys/block/{disk}/size")
    return {"name": read(f"{d}/name"), "manufacturer_id": read(f"{d}/manfid"), "oem": read(f"{d}/oemid"),
            "date": read(f"{d}/date"), "size_gb": round(int(size) * 512 / 1e9, 1) if size and size.isdigit() else None}


def check():
    head("CHECK - is the Pi healthy and set up for 24/7 use?")
    out = []
    model = read("/proc/device-tree/model") or platform.machine()
    os_name = ""
    for line in (read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip('"')
    tz = run(["timedatectl", "show", "-p", "Timezone", "--value"]).stdout.strip()
    say(f"  {model} · {os_name} · kernel {platform.release()} · time zone {tz or '?'}")
    snap = pihealth.snapshot()

    t = snap["temp_c"]
    if t is not None:
        item(out, "Temperature now", "ok" if t < 60 else "warn" if t < 75 else "bad",
             f"{t} °C" + ("" if t < 60 else " (high for idle: check the fan)"))
    fan = pihealth.fan()
    if fan and fan.get("rpm"):
        item(out, "Fan", "ok", f"{fan['rpm']} rpm (fan header)")
    elif fan:
        item(out, "Fan", "info", f"on the fan header, not spinning right now (speed {fan.get('pwm')}/255; "
                                 "it starts as the Pi warms up)")
    else:
        item(out, "Fan", "info", "not on the fan header, so its speed can't be read (fine for a GPIO/case fan "
                                 "- the stress test shows whether it keeps up)")
    flags = snap["throttle_flags"]
    if flags is not None:
        uv = [f for f in flags if "under-voltage" in f]
        item(out, "Throttling since boot", "ok" if not flags else "bad" if uv else "warn",
             "none" if not flags else ", ".join(flags))
    v = pihealth.input_volts()
    if v is not None:
        item(out, "Power input", "ok" if v >= 4.9 else "warn" if v >= 4.75 else "bad", f"{v} V")
    ma = pihealth.psu_max_current_ma()
    if ma is not None:
        item(out, "Power supply says it can give", "ok" if ma >= 5000 else "info",
             f"{ma / 1000:g} A at 5 V" + ("" if ma >= 5000 else
                                           " - fine for Jarvis; the Pi limits its USB ports to 600 mA, which "
                                           "matters only for a USB SSD later (an NVMe HAT is fine)"))
    m = pihealth.memory() or {}
    if m:
        item(out, "Memory", "ok", f"{m['available_mb']} MB free of {m['total_mb']} MB"
             + (f", swap in use {m['swap_used_mb']} MB" if m.get("swap_used_mb") else ""))
    part, disk = pihealth.root_device()
    card = card_info(disk)
    free = pihealth.disk_free()
    item(out, "Storage", "ok" if free["free_pct"] > 15 else "warn",
         f"{disk or '?'}" + (f" (SD card {card['name']}, {card['size_gb']} GB, made {card['date']})" if card else "")
         + f", {free['free_gb']} GB free ({free['free_pct']}%)")
    errs = pihealth.fs_errors(part)
    if errs is not None:
        item(out, "File system errors", "ok" if errs == 0 else "bad", str(errs))
    svc = pihealth.service("jarvis.service") or {}
    ctl = pihealth.service("jarvis-control.service") or {}
    state = svc.get("state", "?")
    item(out, "Jarvis", "ok" if state == "active" else "bad" if state == "failed" else "info", f"{state}"
         + (f", using {svc['mem_mb']} MB" if svc.get("mem_mb") else "")
         + (f", restarted {svc['restarts']}x" if svc.get("restarts") else ""))
    item(out, "Power switch (port 5152)", "ok" if ctl.get("state") == "active" else "info", ctl.get("state", "?"))
    timer = run(["systemctl", "list-timers", "jarvis-nightly.timer", "--no-pager", "--no-legend"]).stdout.strip()
    item(out, "Nightly stop", "ok" if timer else "info", timer.split("  ")[0] if timer else "not installed yet")
    ts = run(["tailscale", "ip", "-4"]).stdout.strip()
    item(out, "Tailscale", "ok" if ts.startswith("100.") else "warn", ts or "not connected")
    save("check", {"model": model, "os": os_name, "timezone": tz, "snapshot": snap, "card": card, "disk": disk,
                   "items": out})
    return out


# ── SD card ───────────────────────────────────────────────────────────────────

def parse_ios(text):
    """`/sys/kernel/debug/mmc0/ios` -> {'clock_mhz', 'timing'}: the bus speed the card runs at."""
    out = {}
    m = re.search(r"actual clock:\s*(\d+)", text or "") or re.search(r"clock:\s*(\d+)", text or "")
    if m:
        out["clock_mhz"] = round(int(m.group(1)) / 1e6)
    m = re.search(r"timing spec:\s*\d+\s*\(([^)]*)\)", text or "")
    if m:
        out["timing"] = m.group(1)
    return out


def parse_fio(text):
    """fio --output-format=json -> {'read_mbs', 'write_mbs', 'read_iops', 'write_iops', 'p99_ms'}"""
    try:
        start = text.index("{")
        job = json.loads(text[start:])["jobs"][0]
    except (ValueError, KeyError, IndexError):
        return None
    out = {}
    for side in ("read", "write"):
        s = job.get(side, {})
        if s.get("io_bytes", 0) > 0:
            out[f"{side}_mbs"] = round(s.get("bw", 0) * 1024 / 1e6, 1)       # bw is KiB/s
            out[f"{side}_iops"] = round(s.get("iops", 0))
            pct = (s.get("clat_ns") or {}).get("percentile") or {}
            p99 = pct.get("99.000000")
            if p99:
                out["p99_ms"] = round(p99 / 1e6, 1)
    return out


def sd_verdict(r):
    """Plain-language verdict for Jarvis's use from the fio numbers."""
    notes, level = [], "ok"
    rr, rw, sync = r.get("rand_read", {}), r.get("rand_write", {}), r.get("sync_write", {})
    if rr.get("read_iops") is not None and rw.get("write_iops") is not None:
        cls = ("A2" if rr["read_iops"] >= A2["read_iops"] and rw["write_iops"] >= A2["write_iops"] else
               "A1" if rr["read_iops"] >= A1["read_iops"] and rw["write_iops"] >= A1["write_iops"] else "below A1")
        notes.append(f"small-file speed measures as {cls} ({rr['read_iops']} reads/s, {rw['write_iops']} writes/s)")
        if cls == "below A1":
            level = "warn"
    if sync.get("write_iops") is not None:
        notes.append(f"saving a small file: {sync['write_iops']}/s, slowest 1% {sync.get('p99_ms', '?')} ms")
        if sync["write_iops"] < 50 or (sync.get("p99_ms") or 0) > 200:
            level = "warn"
            notes.append("slow saves can make Jarvis pause - an A2 card or an SSD would help")
    sw = r.get("seq_write", {}).get("write_mbs")
    if sw is not None:
        notes.append(f"big writes {sw} MB/s" + (" (below Class 10)" if sw < 10 else ""))
    if r.get("kernel_errors"):
        level = "bad"
        notes.append(f"{r['kernel_errors']} SD card errors in the kernel log - back up and replace the card")
    return level, "; ".join(notes)


def sd(size_mb=1024):
    head("SD CARD - speed and reliability for Jarvis")
    if not shutil.which("fio"):
        say("  fio is missing:  sudo apt install -y fio")
        return None
    part, disk = pihealth.root_device()
    card = card_info(disk)
    if card:
        say(f"  Card: {card['name']} · {card['size_gb']} GB · made {card['date']}")
    elif disk:
        say(f"  The system runs from {disk} (not an SD card) - testing it anyway")
    free = pihealth.disk_free()
    if free["free_gb"] * 1000 < size_mb * 3:
        say(f"  Only {free['free_gb']} GB free; using a smaller test file")
        size_mb = max(128, int(free["free_gb"] * 1000 / 4))
    res = {"card": card, "disk": disk, "size_mb": size_mb}
    if disk and disk.startswith("mmcblk"):
        say("  Reading the card's bus speed (sudo)...")
        # the controller (mmc0 / mmc1) isn't always numbered like the disk: take it from sysfs
        m = re.search(r"/mmc_host/(mmc\d+)/", os.path.realpath(f"/sys/block/{disk}/device"))
        host = m.group(1) if m else disk.replace("mmcblk", "mmc")
        ios = run(["sudo", "cat", f"/sys/kernel/debug/{host}/ios"], timeout=60)
        res["bus"] = parse_ios(ios.stdout)
        if res["bus"]:
            say(f"  Bus: {res['bus'].get('timing', '?')} at {res['bus'].get('clock_mhz', '?')} MHz")
    path = os.path.join(os.path.expanduser("~"), ".jarvis_sdtest")
    jobs = [  # (key, label, fio args)
        ("seq_write", "big file write", ["--rw=write", "--bs=1M", "--iodepth=4"]),
        ("seq_read", "big file read", ["--rw=read", "--bs=1M", "--iodepth=4"]),
        ("rand_read", "small random reads", ["--rw=randread", "--bs=4k", "--iodepth=4", "--runtime=30", "--time_based"]),
        ("rand_write", "small random writes", ["--rw=randwrite", "--bs=4k", "--iodepth=4", "--runtime=30",
                                               "--time_based"]),
        # what Jarvis really does: write a small file and wait until it's safely stored
        ("sync_write", "small saves (fsync)", ["--rw=randwrite", "--bs=4k", "--iodepth=1", "--fsync=1",
                                               "--runtime=20", "--time_based"]),
    ]
    try:
        for key, label, args in jobs:
            say(f"  {label}...")
            r = run(["fio", f"--name={key}", f"--filename={path}", f"--size={size_mb}M", "--direct=1",
                     "--ioengine=libaio", "--output-format=json", *args], timeout=600)
            res[key] = parse_fio(r.stdout) or {"error": (r.stderr or "no output").strip()[:200]}
            say(f"     {res[key]}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    klog = run(["journalctl", "-k", "-b", "--no-pager", "-q"]).stdout
    res["kernel_errors"] = len(re.findall(r"mmc\d+:.*(?:error|timeout|timed out)", klog, re.I))
    res["fs_errors"] = pihealth.fs_errors(part)
    level, verdict = sd_verdict(res)
    res["level"], res["verdict"] = level, verdict
    say()
    say(f"  [{level.upper()}] {verdict}")
    save("sd", res)
    return res


# ── stress ────────────────────────────────────────────────────────────────────

HAPPENED = 0xF0000     # the "since boot" throttle bits


def stress_summary(samples, flags_before):
    temps = [s["temp_c"] for s in samples if s.get("temp_c") is not None]
    if not temps:
        return {"error": "no temperature readings"}
    freqs = [s["cpu_mhz"] for s in samples if s.get("cpu_mhz")]
    last = samples[-1].get("throttled_raw") or 0
    new = (last & HAPPENED) & ~(flags_before & HAPPENED)
    tail = temps[-max(1, len(temps) // 8):]
    peak = max(temps)
    out = {"start_c": temps[0], "peak_c": peak, "steady_c": round(sum(tail) / len(tail), 1),
           "seconds_at_80": sum(2 for x in temps if x >= 80), "min_mhz": min(freqs) if freqs else None,
           "max_mhz": max(freqs) if freqs else None,
           "fan_max_rpm": max((s.get("fan_rpm") or 0) for s in samples) or None,
           "new_throttle_flags": [n for b, n in pihealth.THROTTLE_BITS.items() if new & (1 << b)],
           "minutes": round(len(samples) * 2 / 60, 1)}
    out["level"] = "ok" if peak < 75 and not out["new_throttle_flags"] else "warn" if peak < 82 else "bad"
    out["verdict"] = (f"peak {peak} °C, settled at {out['steady_c']} °C"
                      + (", no slowdown" if not out["new_throttle_flags"] else
                         f", slowed down ({', '.join(out['new_throttle_flags'])})")
                      + {"ok": " - cooling is fine for 24/7",
                         "warn": " - OK, but little headroom on a hot day",
                         "bad": " - the cooling can't keep up; improve airflow before leaving it on"}[out["level"]])
    return out


def stress(minutes=15):
    head(f"STRESS - all 4 cores at 100% for {minutes} min")
    if not shutil.which("stress-ng"):
        say("  stress-ng is missing:  sudo apt install -y stress-ng")
        return None
    say("  Jarvis can keep running; this is far more than he ever uses. Ctrl+C stops early.")
    t0 = pihealth.throttled()
    before = t0[0] if t0 else 0
    proc = subprocess.Popen(["stress-ng", "--cpu", "4", "--timeout", f"{minutes}m", "--quiet"])
    samples = []
    try:
        while proc.poll() is None:
            t = pihealth.throttled()
            s = {**pihealth.snapshot(), "throttled_raw": t[0] if t else 0}
            samples.append(s)
            sys.stdout.write(f"\r  {len(samples) * 2 // 60:>2} min  {s['temp_c']} °C  {s['cpu_mhz']} MHz  "
                             f"fan {s['fan_rpm'] or '-'} rpm   ")
            sys.stdout.flush()
            if (s["temp_c"] or 0) >= 85:
                say("\n  85 °C reached - stopping early")
                break
            time.sleep(2)
    except KeyboardInterrupt:
        say("\n  Stopped early.")
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    say()
    res = stress_summary(samples, before)
    res["samples"] = samples[::5]
    say(f"  [{res.get('level', 'bad').upper()}] {res.get('verdict', res.get('error'))}")
    save("stress", res)
    return res


# ── load: real commands to the running Jarvis ─────────────────────────────────

def _open(url, data=None, headers=None, timeout=60):
    ctx = ssl.create_default_context()
    if "127.0.0.1" in url or "localhost" in url:
        ctx.check_hostname = False              # the certificate is for the Tailscale name
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout, context=ctx if url.startswith("https") else None)


def find_jarvis(url=None):
    for u in ([url] if url else ["https://127.0.0.1:5151", "http://127.0.0.1:5151"]):
        try:
            _open(u + "/status?files=0", timeout=5).read()
            return u
        except OSError:
            continue
    return None


def multipart(field, filename, data, ctype):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def jarvis_log(url):
    return json.loads(_open(url + "/status?files=0", timeout=10).read()).get("log", [])


def new_replies(before, log):
    seen = {(e.get("role"), e.get("time"), e.get("sample") or e.get("text")) for e in before}
    return [e["text"] for e in log if e.get("role") == "jarvis"
            and (e.get("role"), e.get("time"), e.get("sample") or e.get("text")) not in seen]


class Sampler(threading.Thread):
    """temp / CPU% / Jarvis memory every second while a test runs."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self.stop_flag = [], threading.Event()

    def run(self):
        prev = pihealth.cpu_times()
        while not self.stop_flag.wait(1.0):
            cur = pihealth.cpu_times()
            svc = pihealth.service("jarvis.service") or {}
            self.samples.append({"temp_c": pihealth.temp_c(), "cpu_pct": pihealth.cpu_percent(prev, cur),
                                 "jarvis_mem_mb": svc.get("mem_mb")})
            prev = cur


def load_summary(turns, samples):
    ok = [t for t in turns if t.get("reply_s") is not None]
    temps = [s["temp_c"] for s in samples if s.get("temp_c") is not None]
    cpus = [s["cpu_pct"] for s in samples if s.get("cpu_pct") is not None]
    mems = [s["jarvis_mem_mb"] for s in samples if s.get("jarvis_mem_mb") is not None]
    lat = sorted(t["reply_s"] for t in ok)
    out = {"commands": len(turns), "answered": len(ok),
           "median_reply_s": lat[len(lat) // 2] if lat else None, "slowest_reply_s": lat[-1] if lat else None,
           "peak_c": max(temps) if temps else None, "avg_cpu_pct": round(sum(cpus) / len(cpus), 1) if cpus else None,
           "peak_cpu_pct": max(cpus) if cpus else None,
           "jarvis_mem_start_mb": mems[0] if mems else None, "jarvis_mem_end_mb": mems[-1] if mems else None,
           "jarvis_mem_peak_mb": max(mems) if mems else None}
    out["level"] = ("bad" if len(ok) < len(turns) * 0.8 else
                    "warn" if (out["median_reply_s"] or 0) > 10 or (out["peak_c"] or 0) >= 75 else "ok")
    out["verdict"] = (f"{len(ok)}/{len(turns)} answered, typical reply {out['median_reply_s']} s "
                      f"(slowest {out['slowest_reply_s']} s), CPU avg {out['avg_cpu_pct']}% / peak "
                      f"{out['peak_cpu_pct']}%, peak {out['peak_c']} °C, Jarvis {out['jarvis_mem_start_mb']} → "
                      f"{out['jarvis_mem_end_mb']} MB")
    return out


def load_test(rounds=10, url=None, yes=False):
    head(f"LOAD - {rounds} real spoken commands to the running Jarvis")
    url = find_jarvis(url)
    if not url:
        say("  Jarvis isn't answering on port 5151. Start him (power page, or python3 scripts/pi/control.py start).")
        return None
    if not shutil.which("espeak-ng"):
        say("  espeak-ng is missing (it speaks the test commands):  sudo apt install -y espeak-ng")
        return None
    say(f"  Jarvis at {url}. Each command is a normal request: it costs a few API tokens and shows up")
    say("  in Jarvis's conversation history.")
    if not yes and input("  Go ahead? [y/N] ").strip().lower() != "y":
        return None
    sampler = Sampler()
    sampler.start()
    turns = []
    tmp = tempfile.mkdtemp()
    try:
        for i in range(rounds):
            text = LOAD_COMMANDS[i % len(LOAD_COMMANDS)]
            wav = os.path.join(tmp, "cmd.wav")
            run(["espeak-ng", "-s", "150", "-w", wav, text])
            with open(wav, "rb") as f:
                body, hdrs = multipart("audio", "command.wav", f.read(), "audio/wav")
            t = {"said": text}
            try:
                before = jarvis_log(url)
                t0 = time.time()
                resp = json.loads(_open(url + "/phone-command", data=body, headers=hdrs, timeout=60).read())
                t["heard"] = resp.get("command")
                t["stt_s"] = round(time.time() - t0, 2)
                while time.time() - t0 < 60:
                    time.sleep(0.5)
                    got = new_replies(before, jarvis_log(url))
                    if got:
                        t["reply_s"] = round(time.time() - t0, 2)
                        t["reply"] = got[0][:120]
                        break
            except (OSError, ValueError) as e:
                t["error"] = str(e)[:120]
            turns.append(t)
            say(f"  {i + 1:>2}. \"{text}\" -> heard \"{t.get('heard')}\" · reply in {t.get('reply_s', 'no reply')} s")
            time.sleep(3)
    except KeyboardInterrupt:
        say("  Stopped early.")
    finally:
        sampler.stop_flag.set()
        sampler.join(timeout=3)
        shutil.rmtree(tmp, ignore_errors=True)
    res = load_summary(turns, sampler.samples)
    res["turns"] = turns
    say(f"\n  [{res['level'].upper()}] {res['verdict']}")
    save("load", res)
    return res


# ── soak: everything, every minute, for a day ─────────────────────────────────

SOAK_FIELDS = ["time", "temp_c", "fan_rpm", "throttled", "cpu_mhz", "load1", "cpu_pct", "mem_available_mb",
               "jarvis", "jarvis_mem_mb", "jarvis_restarts", "disk_written_mb"]

ENDURANCE_WRITES = 200     # rough: full-card writes a consumer microSD survives (TLC ~300+, QLC ~100)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def soak_summary(rows, card_gb=None):
    if len(rows) < 2:
        return {"error": "not enough samples"}
    temps = [_f(r["temp_c"]) for r in rows if _f(r["temp_c"]) is not None]
    t0, t1 = rows[0]["time"], rows[-1]["time"]
    hours = (time.mktime(time.strptime(t1, "%Y-%m-%d %H:%M:%S"))
             - time.mktime(time.strptime(t0, "%Y-%m-%d %H:%M:%S"))) / 3600
    interval_min = hours * 60 / max(1, len(rows) - 1)
    now_bits = 0xF
    throttled_samples = sum(1 for r in rows if r.get("throttled") and int(r["throttled"], 16) & now_bits)
    # Jarvis memory trend while running: least-squares slope, MB per hour
    pts = [(i * interval_min / 60, _f(r["jarvis_mem_mb"])) for i, r in enumerate(rows)
           if r.get("jarvis") == "active" and _f(r["jarvis_mem_mb"]) is not None]
    slope = None
    if len(pts) >= 10:
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        den = sum((x - mx) ** 2 for x, _ in pts)
        slope = round(sum((x - mx) * (y - my) for x, y in pts) / den, 2) if den else None
    w0, w1 = _f(rows[0]["disk_written_mb"]), _f(rows[-1]["disk_written_mb"])
    per_day = round((w1 - w0) / hours * 24, 1) if w0 is not None and w1 is not None and hours > 0 and w1 >= w0 else None
    r0, r1 = _f(rows[0]["jarvis_restarts"]), _f(rows[-1]["jarvis_restarts"])
    states = {}
    for r in rows:
        states[r.get("jarvis") or "?"] = states.get(r.get("jarvis") or "?", 0) + 1
    out = {"hours": round(hours, 1), "samples": len(rows),
           "temp_min_c": min(temps) if temps else None, "temp_avg_c": round(sum(temps) / len(temps), 1) if temps else None,
           "temp_max_c": max(temps) if temps else None,
           "minutes_over_70c": round(sum(1 for t in temps if t >= 70) * interval_min),
           "throttled_minutes": round(throttled_samples * interval_min),
           "jarvis_running_pct": round(100 * states.get("active", 0) / len(rows)),
           "jarvis_restarts": int(r1 - r0) if r0 is not None and r1 is not None and r1 >= r0 else None,
           "jarvis_mem_slope_mb_per_h": slope,
           "jarvis_mem_max_mb": max((y for _, y in pts), default=None),
           "disk_writes_mb_per_day": per_day}
    if per_day and card_gb:
        out["sd_wear_years_rough"] = round(card_gb * 1000 * ENDURANCE_WRITES / per_day / 365, 1)
    problems = []
    if (out["temp_max_c"] or 0) >= 80 or out["throttled_minutes"]:
        problems.append("ran hot / slowed down")
    if out["jarvis_restarts"]:
        problems.append(f"Jarvis restarted {out['jarvis_restarts']}x (crashes: journalctl -u jarvis)")
    if slope is not None and slope > 5:
        problems.append(f"Jarvis's memory keeps growing ({slope} MB/hour)")
    out["level"] = "bad" if len(problems) > 1 else "warn" if problems else "ok"
    out["verdict"] = (f"{out['hours']} h: {out['temp_min_c']}-{out['temp_max_c']} °C (avg {out['temp_avg_c']}), "
                      f"Jarvis running {out['jarvis_running_pct']}% of the time, memory trend "
                      f"{slope if slope is not None else '?'} MB/h, {per_day if per_day is not None else '?'} MB "
                      "written per day" + (f" (SD wear: roughly {out['sd_wear_years_rough']} years)"
                                           if out.get("sd_wear_years_rough") else "")
                      + (" - " + "; ".join(problems) if problems else " - all steady"))
    return out


def soak(hours=24.0, interval=60):
    head(f"SOAK - log every {interval} s for {hours} h")
    if not os.environ.get("TMUX") and not os.environ.get("STY") and sys.stdin.isatty():
        say("  !! Not inside tmux: closing SSH stops this. Better:")
        say(f"     tmux new -s soak './scripts/pi/diag soak --hours {hours:g}'   (detach: Ctrl+B then D)")
        if input("  Run here anyway? [y/N] ").strip().lower() != "y":
            return None
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"soak-{time.strftime('%Y%m%d-%H%M')}.csv")
    _, disk = pihealth.root_device()
    end = time.time() + hours * 3600
    prev = pihealth.cpu_times()
    say(f"  Writing {os.path.relpath(path, ROOT)} - Ctrl+C stops and summarizes.")
    try:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SOAK_FIELDS)
            w.writeheader()
            while time.time() < end:
                cur = pihealth.cpu_times()
                snap = pihealth.snapshot()
                svc = pihealth.service("jarvis.service") or {}
                row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "temp_c": snap["temp_c"], "fan_rpm": snap["fan_rpm"],
                       "throttled": snap["throttled"], "cpu_mhz": snap["cpu_mhz"], "load1": snap["load1"],
                       "cpu_pct": pihealth.cpu_percent(prev, cur), "mem_available_mb": snap["mem_available_mb"],
                       "jarvis": svc.get("state"), "jarvis_mem_mb": svc.get("mem_mb"),
                       "jarvis_restarts": svc.get("restarts"), "disk_written_mb": pihealth.disk_written_mb(disk)}
                w.writerow(row)
                f.flush()
                prev = cur
                sys.stdout.write(f"\r  {row['time']}  {row['temp_c']} °C  CPU {row['cpu_pct']}%  "
                                 f"Jarvis {row['jarvis']} {row['jarvis_mem_mb'] or '-'} MB   ")
                sys.stdout.flush()
                time.sleep(max(1, interval - (time.time() % interval)))
    except KeyboardInterrupt:
        say("\n  Stopped.")
    return summarize_soak(path)


def summarize_soak(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    card = (load("check") or {}).get("card") or card_info(pihealth.root_device()[1]) or {}
    res = soak_summary(rows, card.get("size_gb"))
    res["file"] = os.path.relpath(path, ROOT)
    say()
    say(f"  [{res.get('level', 'bad').upper()}] {res.get('verdict', res.get('error'))}")
    save("soak", res)
    return res


# ── report ────────────────────────────────────────────────────────────────────

def write_report():
    parts = [f"# Pi 5 health report - {time.strftime('%Y-%m-%d %H:%M')}", ""]
    chk = load("check")
    if chk:
        parts += [f"**{chk.get('model', '?')}** · {chk.get('os', '?')} · time zone {chk.get('timezone', '?')}", "",
                  "| Check | | Result |", "|---|---|---|"]
        parts += [f"| {i['name']} | {i['level'].upper()} | {i['detail']} |" for i in chk.get("items", [])]
        parts.append("")
    for name, title in (("sd", "SD card"), ("stress", "Stress (4 cores, 100%)"), ("load", "Load (real commands)"),
                        ("soak", "Soak (long run)")):
        r = load(name)
        if r:
            parts += [f"## {title} - {r.get('level', '?').upper()}", r.get("verdict") or r.get("error", ""),
                      "", "<details><summary>numbers</summary>", "", "```json",
                      json.dumps({k: v for k, v in r.items() if k not in ("samples", "turns")}, indent=1)[:3000],
                      "```", "</details>", ""]
        else:
            parts += [f"## {title} - not run", ""]
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description="Pi 5 health checks for Jarvis")
    ap.add_argument("step", choices=["check", "sd", "stress", "load", "soak", "report", "selftest"])
    ap.add_argument("--minutes", type=float, default=15, help="stress: how long")
    ap.add_argument("--rounds", type=int, default=10, help="load: how many commands")
    ap.add_argument("--hours", type=float, default=24, help="soak: how long")
    ap.add_argument("--interval", type=int, default=60, help="soak: seconds between samples")
    ap.add_argument("--size-mb", type=int, default=1024, help="sd: test file size")
    ap.add_argument("--url", help="load: Jarvis address (default: this Pi)")
    ap.add_argument("--summarize", help="soak: summarize an existing CSV instead of recording")
    ap.add_argument("--yes", action="store_true", help="load: don't ask before sending commands")
    a = ap.parse_args()
    if a.step == "selftest":
        from diag_selftest import selftest
        sys.exit(0 if selftest() else 1)
    if a.step == "check":
        check()
    elif a.step == "sd":
        sd(a.size_mb)
    elif a.step == "stress":
        stress(a.minutes)
    elif a.step == "load":
        load_test(a.rounds, a.url, a.yes)
    elif a.step == "soak":
        summarize_soak(a.summarize) if a.summarize else soak(a.hours, a.interval)
    if a.step != "report":
        say()
    path = write_report()
    say(f"Report: {os.path.relpath(path, ROOT)}  (cat it and paste it to Claude)")


if __name__ == "__main__":
    main()
