"""
Raspberry Pi health readings shared by control.py (power switch + heat guard) and diagnose.py.
Standard library only. Every reader returns None when the value isn't available (another board,
a missing tool), and the parsers take text so diagnose.py's self-test can run them anywhere.
"""

import glob
import os
import re
import shutil
import subprocess


def _run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ── temperature, throttling, power ────────────────────────────────────────────

def temp_c():
    v = _read("/sys/class/thermal/thermal_zone0/temp")
    return round(int(v) / 1000, 1) if v and v.lstrip("-").isdigit() else None


THROTTLE_BITS = {
    0: "under-voltage now", 1: "CPU speed capped now", 2: "throttled now", 3: "soft temperature limit now",
    16: "under-voltage happened", 17: "CPU speed capped happened", 18: "throttling happened",
    19: "soft temperature limit happened",
}


def parse_throttled(text):
    """'throttled=0x50005' -> (0x50005, ['under-voltage now', ...]); None if unreadable."""
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", text or "")
    if not m:
        return None
    value = int(m.group(1), 16)
    return value, [name for bit, name in sorted(THROTTLE_BITS.items()) if value & (1 << bit)]


def throttled():
    return parse_throttled(_run(["vcgencmd", "get_throttled"]))


def parse_volts(text):
    """'EXT5V_V volt(24)=5.12400000V' -> 5.124"""
    m = re.search(r"=\s*([0-9.]+)\s*V", text or "")
    return round(float(m.group(1)), 3) if m else None


def input_volts():
    """The 5 V input as the Pi 5's power chip measures it (None on older boards)."""
    return parse_volts(_run(["vcgencmd", "pmic_read_adc", "EXT5V_V"]))


def psu_max_current_ma():
    """What the power supply told the Pi 5 it can deliver (5000 = official 27 W supply).
    Other supplies usually report 3000, and the Pi then limits its USB ports to 600 mA."""
    try:
        with open("/proc/device-tree/chosen/power/max_current", "rb") as f:
            raw = f.read(4)
        return int.from_bytes(raw, "big") if len(raw) == 4 else None
    except OSError:
        return None


def fan():
    """{'rpm', 'pwm'} for the Pi 5 fan header (Active Cooler / case fan), or None."""
    for d in glob.glob("/sys/devices/platform/cooling_fan/hwmon/hwmon*"):
        rpm, pwm = _read(os.path.join(d, "fan1_input")), _read(os.path.join(d, "pwm1"))
        return {"rpm": int(rpm) if rpm and rpm.isdigit() else None,
                "pwm": int(pwm) if pwm and pwm.isdigit() else None}
    return None


def cpu_mhz():
    v = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    return int(v) // 1000 if v and v.isdigit() else None


# ── CPU, memory, disk ────────────────────────────────────────────────────────

def parse_cpu_times(text):
    """First line of /proc/stat -> (busy, total) jiffies."""
    for line in (text or "").splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            return sum(v) - idle, sum(v)
    return None


def cpu_times():
    return parse_cpu_times(_read("/proc/stat"))


def cpu_percent(before, after):
    if not before or not after or after[1] <= before[1]:
        return None
    return round(100 * (after[0] - before[0]) / (after[1] - before[1]), 1)


def parse_meminfo(text):
    """-> {'total_mb', 'available_mb', 'swap_used_mb'}"""
    kb = {}
    for line in (text or "").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            kb[m.group(1)] = int(m.group(2))
    if "MemTotal" not in kb:
        return None
    return {"total_mb": kb["MemTotal"] // 1024, "available_mb": kb.get("MemAvailable", 0) // 1024,
            "swap_used_mb": (kb.get("SwapTotal", 0) - kb.get("SwapFree", 0)) // 1024}


def memory():
    return parse_meminfo(_read("/proc/meminfo"))


def disk_of(part):
    """'mmcblk0p2' -> 'mmcblk0', 'nvme0n1p2' -> 'nvme0n1', 'sda2' -> 'sda'"""
    m = re.match(r"^((?:mmcblk|nvme\d+n)\d+)p\d+$", part)
    return m.group(1) if m else re.sub(r"\d+$", "", part)


def root_device():
    """('mmcblk0p2', 'mmcblk0'): the partition / is mounted from, and its disk."""
    src = _run(["findmnt", "-n", "-o", "SOURCE", "/"]).strip()
    part = os.path.basename(src) if src.startswith("/dev/") else None
    return (part, disk_of(part)) if part else (None, None)


def parse_block_stat(text):
    """/sys/block/<disk>/stat -> MB written since boot (field 7 = sectors written, 512 B each)."""
    f = (text or "").split()
    return round(int(f[6]) * 512 / 1e6, 1) if len(f) > 6 else None


def disk_written_mb(disk):
    return parse_block_stat(_read(f"/sys/block/{disk}/stat")) if disk else None


def disk_free():
    u = shutil.disk_usage("/")
    return {"total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1),
            "free_pct": round(100 * u.free / u.total, 1)}


def fs_errors(part):
    v = _read(f"/sys/fs/ext4/{part}/errors_count") if part else None
    return int(v) if v and v.isdigit() else None


def uptime_h():
    v = _read("/proc/uptime")
    return round(float(v.split()[0]) / 3600, 2) if v else None


# ── the Jarvis service ────────────────────────────────────────────────────────

def parse_show(text):
    """`systemctl show -p A -p B` output -> {'A': ..., 'B': ...}"""
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def service(unit="jarvis.service"):
    """{'state', 'since', 'mem_mb', 'cpu_s', 'restarts', 'pid'} from systemd (whole unit,
    child processes such as Claude Code runs included)."""
    p = parse_show(_run(["systemctl", "show", unit, "-p", "ActiveState", "-p", "ActiveEnterTimestamp",
                         "-p", "MemoryCurrent", "-p", "CPUUsageNSec", "-p", "NRestarts", "-p", "MainPID",
                         "-p", "LoadState"]))
    if not p:
        return None

    def num(k):
        v = p.get(k, "")
        return int(v) if v.isdigit() else None

    mem, cpu = num("MemoryCurrent"), num("CPUUsageNSec")
    return {"state": p.get("ActiveState") if p.get("LoadState") != "not-found" else "not installed",
            "since": p.get("ActiveEnterTimestamp") or None,
            "mem_mb": round(mem / 1e6, 1) if mem is not None and mem < 1 << 60 else None,
            "cpu_s": round(cpu / 1e9, 1) if cpu is not None and cpu < 1 << 62 else None,
            "restarts": num("NRestarts"), "pid": num("MainPID") or None}


def snapshot():
    """Everything cheap, in one dict (control.py's status, diagnose.py's samples)."""
    t = throttled()
    f = fan()
    m = memory()
    return {"temp_c": temp_c(), "fan_rpm": (f or {}).get("rpm"), "fan_pwm": (f or {}).get("pwm"),
            "throttled": hex(t[0]) if t else None, "throttle_flags": t[1] if t else None,
            "cpu_mhz": cpu_mhz(), "load1": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None,
            "mem_available_mb": (m or {}).get("available_mb"), "mem_total_mb": (m or {}).get("total_mb"),
            "uptime_h": uptime_h()}
