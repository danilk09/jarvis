#!/usr/bin/env python3
"""
Offline self-test for scripts/pi (diagnose.py, pihealth.py, control.py): no Pi needed, any OS.

    ./scripts/pi/diag selftest        (or: python3 scripts/pi/diag_selftest.py)

Feeds sample vcgencmd / /proc / fio / systemctl text through the parsers, checks the stress,
load and soak summaries and the report, and runs the power switch's web server on localhost
with the start/stop calls faked (key checks, status, buttons).
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import control  # noqa: E402
import diagnose  # noqa: E402
import pihealth  # noqa: E402

_results = []


def check(name, cond, detail=""):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  ({detail})"), flush=True)


def section(title):
    print(f"\n{title}", flush=True)


def test_health_parsers():
    section("Pi readings (vcgencmd, /proc, /sys, systemctl)")
    t = pihealth.parse_throttled("throttled=0x50005")
    check("throttled 0x50005 decoded", t and t[0] == 0x50005 and t[1] == [
        "under-voltage now", "throttled now", "under-voltage happened", "throttling happened"], t)
    check("throttled 0x0 -> no flags", pihealth.parse_throttled("throttled=0x0") == (0, []))
    check("throttled garbage -> None", pihealth.parse_throttled("error") is None)
    check("input volts", pihealth.parse_volts("     EXT5V_V volt(24)=5.12400000V") == 5.124)
    mem = pihealth.parse_meminfo("MemTotal:        8245592 kB\nMemFree:  1000 kB\nMemAvailable:    6234112 kB\n"
                                 "SwapTotal:        524272 kB\nSwapFree:         520176 kB\n")
    check("meminfo", mem == {"total_mb": 8052, "available_mb": 6088, "swap_used_mb": 4}, mem)
    a = pihealth.parse_cpu_times("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3")
    b = pihealth.parse_cpu_times("cpu  150 0 150 800 100 0 0 0 0 0\n")
    check("CPU % from /proc/stat", pihealth.cpu_percent(a, b) == 50.0, (a, b))
    check("CPU % needs two samples", pihealth.cpu_percent(None, b) is None)
    check("MB written from block stat",
          pihealth.parse_block_stat("  4523   1234  356789  2345  98765  4321  2000000  56789  0  12345  67890") == 1024.0)
    check("disk of a partition", (pihealth.disk_of("mmcblk0p2"), pihealth.disk_of("nvme0n1p2"),
                                  pihealth.disk_of("sda2")) == ("mmcblk0", "nvme0n1", "sda"))
    show = pihealth.parse_show("ActiveState=active\nMemoryCurrent=412356608\nCPUUsageNSec=[not set]\nNRestarts=2\n")
    check("systemctl show parsed", show["ActiveState"] == "active" and show["NRestarts"] == "2", show)


def test_sd():
    section("SD card (fio, bus speed, verdict)")
    ios = "clock:\t\t200000000 Hz\nactual clock:\t200000000 Hz\nvdd:\t\t21 (3.3 ~ 3.4 V)\n" \
          "bus width:\t2 (4 bits)\ntiming spec:\t6 (sd uhs SDR104)\nsignal voltage:\t1 (1.80 V)\n"
    check("bus speed from debugfs", diagnose.parse_ios(ios) == {"clock_mhz": 200, "timing": "sd uhs SDR104"},
          diagnose.parse_ios(ios))
    fio = "fio: note: both iodepth >= 1 and synchronous I/O engine...\n" + json.dumps({"jobs": [{
        "jobname": "rand_read", "read": {"io_bytes": 9_000_000, "bw": 7000, "iops": 1750.4,
                                         "clat_ns": {"percentile": {"99.000000": 3_500_000}}},
        "write": {"io_bytes": 0, "bw": 0, "iops": 0}}]})
    check("fio json (with a warning line first)", diagnose.parse_fio(fio) ==
          {"read_mbs": 7.2, "read_iops": 1750, "p99_ms": 3.5}, diagnose.parse_fio(fio))
    check("fio garbage -> None", diagnose.parse_fio("fio: failed") is None)
    good = {"seq_write": {"write_mbs": 45.0}, "rand_read": {"read_iops": 4200}, "rand_write": {"write_iops": 2100},
            "sync_write": {"write_iops": 400, "p99_ms": 12.0}, "kernel_errors": 0}
    level, text = diagnose.sd_verdict(good)
    check("fast card -> ok, rated A2", level == "ok" and "A2" in text, (level, text))
    slow = dict(good, rand_write={"write_iops": 120}, sync_write={"write_iops": 30, "p99_ms": 350.0})
    level, text = diagnose.sd_verdict(slow)
    check("slow saves -> warn, below A1", level == "warn" and "below A1" in text and "pause" in text, (level, text))
    level, _ = diagnose.sd_verdict(dict(good, kernel_errors=3))
    check("kernel mmc errors -> bad", level == "bad")


def test_summaries():
    section("stress / load / soak summaries")
    samples = [{"temp_c": 45 + i, "cpu_mhz": 2400, "fan_rpm": 3000 + i * 10, "throttled_raw": 0} for i in range(20)]
    s = diagnose.stress_summary(samples, 0)
    check("stress: cool run -> ok", s["level"] == "ok" and s["peak_c"] == 64 and not s["new_throttle_flags"], s)
    hot = [{"temp_c": 70 + i, "cpu_mhz": 1500, "throttled_raw": 0xE0000 if i > 10 else 0} for i in range(15)]
    s = diagnose.stress_summary(hot, 0x0)
    check("stress: 84 C + throttling -> bad", s["level"] == "bad" and "throttling happened" in s["new_throttle_flags"]
          and s["seconds_at_80"] == 10, s)
    s = diagnose.stress_summary(hot, 0xE0000)
    check("stress: throttle bits already set before don't count", s["new_throttle_flags"] == [], s)

    turns = [{"reply_s": x} for x in (2.1, 3.0, 2.5, 9.0)] + [{"error": "timeout"}]
    samp = [{"temp_c": 50, "cpu_pct": 30, "jarvis_mem_mb": 400}, {"temp_c": 55, "cpu_pct": 90, "jarvis_mem_mb": 430}]
    s = diagnose.load_summary(turns, samp)
    check("load: 4/5 answered, median 3.0 s", s["answered"] == 4 and s["median_reply_s"] == 3.0
          and s["peak_cpu_pct"] == 90 and s["level"] == "ok", s)

    def rows(n, mem_step=0.0, restarts_at=None, temp=55, written_per_row=1.5):
        out = []
        t0 = time.mktime(time.strptime("2026-10-21 08:00:00", "%Y-%m-%d %H:%M:%S"))
        for i in range(n):
            out.append({"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 + 60 * i)),
                        "temp_c": str(temp + (i % 5)), "throttled": "0x0", "jarvis": "active",
                        "jarvis_mem_mb": str(400 + mem_step * i), "jarvis_restarts": "1" if restarts_at
                        and i >= restarts_at else "0", "disk_written_mb": str(1000 + written_per_row * i)})
        return out

    s = diagnose.soak_summary(rows(241), card_gb=256)
    check("soak: steady 4 h -> ok", s["level"] == "ok" and s["hours"] == 4.0 and s["jarvis_restarts"] == 0
          and abs(s["jarvis_mem_slope_mb_per_h"]) < 0.1, s)
    check("soak: writes per day and SD wear estimate", s["disk_writes_mb_per_day"] == 2160.0
          and s["sd_wear_years_rough"] == 64.9, s)
    s = diagnose.soak_summary(rows(241, mem_step=0.5), card_gb=256)
    check("soak: memory growing 30 MB/h -> warn", s["level"] == "warn" and s["jarvis_mem_slope_mb_per_h"] == 30.0, s)
    s = diagnose.soak_summary(rows(241, mem_step=0.5, restarts_at=100, temp=80), card_gb=256)
    check("soak: leak + restart + heat -> bad", s["level"] == "bad" and s["jarvis_restarts"] == 1
          and s["minutes_over_70c"] == 241, s)
    check("soak: too few samples", "error" in diagnose.soak_summary(rows(1)))


def test_report(tmp):
    section("report")
    real = diagnose.RESULTS
    diagnose.RESULTS = tmp
    try:
        diagnose.save("check", {"model": "Raspberry Pi 5 Model B Rev 1.1", "os": "Debian GNU/Linux 13 (trixie)",
                                "timezone": "Pacific/Honolulu",
                                "items": [{"name": "Temperature now", "level": "ok", "detail": "48.2 °C"}]})
        diagnose.save("sd", {"level": "warn", "verdict": "small-file speed measures as below A1", "rand_read": {}})
        path = diagnose.write_report()
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for frag in ("Raspberry Pi 5", "Pacific/Honolulu", "| Temperature now | OK | 48.2 °C |",
                     "## SD card - WARN", "below A1", "## Stress (4 cores, 100%) - not run", "## Soak (long run) - not run"):
            check(f"report contains {frag!r}", frag in text)
    finally:
        diagnose.RESULTS = real


def test_control(tmp):
    section("power switch (control.py) on localhost, systemctl faked")
    check("reads NTFY_SERVER from core/config.py", control._config_value("NTFY_SERVER", None) == "https://ntfy.sh")
    check("missing setting -> default", control._config_value("NO_SUCH_SETTING", 7) == 7)
    check("page has Start/Stop and links to Jarvis's phone page",
          'id="start"' in control.PAGE and 'id="stop"' in control.PAGE and ":5151/phone" in control.PAGE)

    real = control.STATE_FILE, control.set_jarvis, control.status
    control.STATE_FILE = os.path.join(tmp, "control.json")
    calls = []
    control.set_jarvis = lambda on, why: (calls.append((on, why)), (True, "started" if on else "stopped"))[1]
    control.status = lambda: {"jarvis": "active", "temp_c": 51.0}
    server = control.ThreadingHTTPServer(("127.0.0.1", 0), control.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def req(path, method="GET", key=None):
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                   headers={"X-Key": key} if key else {}, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    try:
        key = control.load_state()["key"]
        check("first run: key made, Jarvis wanted on", len(key) > 16 and control.load_state()["desired"] == "on")
        code, body = req("/")
        check("page served without a key", code == 200 and "Jarvis Power" in body)
        check("status without key -> 403", req("/api/status")[0] == 403)
        check("status with a wrong key -> 403", req("/api/status", key="nope")[0] == 403)
        code, body = req("/api/status", key=key)
        check("status with the key", code == 200 and json.loads(body)["jarvis"] == "active", body)
        check("stop without key refused, nothing done", req("/api/stop", "POST")[0] == 403 and not calls)
        code, body = req("/api/stop", "POST", key)
        check("stop with key -> Jarvis stopped", code == 200 and json.loads(body)["ok"] and calls == [(False, "phone button")])
        req("/api/start", "POST", key)
        check("start with key", calls[-1] == (True, "phone button"))
        check("unknown path -> 404", req("/api/reboot", "POST", key)[0] == 404)
    finally:
        server.shutdown()
        control.STATE_FILE, control.set_jarvis, control.status = real


def selftest():
    print("Pi diagnostics self-test (offline)")
    with tempfile.TemporaryDirectory() as tmp:
        for t in (test_health_parsers, test_sd, test_summaries, lambda: test_report(tmp), lambda: test_control(tmp)):
            try:
                t()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(f"{getattr(t, '__name__', 'test')} crashed", False, e)
    passed, total = sum(_results), len(_results)
    print(f"\n{passed}/{total} checks passed - {'PASS' if passed == total else 'FAIL'}")
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if selftest() else 1)
