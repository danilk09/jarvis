#!/usr/bin/env python3
"""
Route Bluetooth SCO (call / headset-mode) audio over HCI on a Raspberry Pi.

The Pi's onboard Broadcom/Cypress Bluetooth chip sends SCO audio to its PCM pins by
default, not over the HCI link to Linux. A2DP music works, but the headset microphone
(HFP) arrives as silence. This sends the chip's vendor command
Write_SCO_PCM_Int_Param (OGF 0x3F, OCF 0x01C) with routing = "transport (HCI)",
the same thing `hcitool cmd 0x3f 0x01c 0x01 0x02 0x00 0x01 0x01` does.

Needs root (raw HCI socket). Run once after Bluetooth starts; the
jarvis-bt-sco.service unit installed by setup_bench.sh does that at every boot.

    sudo python3 scripts/pocket/sco_route.py [--dev 0]
"""

import argparse
import shutil
import socket
import struct
import subprocess
import sys
import time

OGF, OCF = 0x3F, 0x01C
PARAMS = bytes([0x01,   # SCO routing: 0x00 = PCM pins, 0x01 = transport (HCI)
                0x02,   # PCM interface rate (ignored for HCI routing)
                0x00,   # frame type
                0x01,   # sync mode
                0x01])  # clock mode

SOL_HCI, HCI_FILTER = 0, 2
HCI_COMMAND_PKT, HCI_EVENT_PKT = 0x01, 0x04
EVT_CMD_COMPLETE, EVT_CMD_STATUS = 0x0E, 0x0F


def send_raw(dev):
    """Send the command on a raw HCI socket and wait for Command Complete.
    Returns (ok, message)."""
    opcode = (OGF << 10) | OCF
    packet = struct.pack("<BHB", HCI_COMMAND_PKT, opcode, len(PARAMS)) + PARAMS
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    try:
        s.bind((dev,))
        # struct hci_ufilter: u32 type_mask, u32 event_mask[2], le16 opcode (+2 padding)
        flt = struct.pack("<IIIH2x", 1 << HCI_EVENT_PKT,
                          (1 << EVT_CMD_COMPLETE) | (1 << EVT_CMD_STATUS), 0, 0)
        s.setsockopt(SOL_HCI, HCI_FILTER, flt)
        s.settimeout(2.0)
        s.send(packet)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                data = s.recv(260)
            except socket.timeout:
                break
            if len(data) < 3 or data[0] != HCI_EVENT_PKT:
                continue
            evt = data[1]
            if evt == EVT_CMD_COMPLETE and len(data) >= 7:
                got = struct.unpack("<H", data[4:6])[0]
                if got == opcode:
                    status = data[6]
                    return status == 0, f"command complete, status 0x{status:02x}"
            if evt == EVT_CMD_STATUS and len(data) >= 7:
                got = struct.unpack("<H", data[5:7])[0]
                if got == opcode and data[3] != 0:
                    return False, f"command status 0x{data[3]:02x}"
        return False, "no reply from the controller"
    finally:
        s.close()


def send_hcitool(dev):
    exe = shutil.which("hcitool")
    if not exe:
        return False, "hcitool not installed"
    args = [exe, "-i", f"hci{dev}", "cmd", f"0x{OGF:02x}", f"0x{OCF:03x}"] + [f"0x{b:02x}" for b in PARAMS]
    r = subprocess.run(args, capture_output=True, text=True, timeout=5)
    return r.returncode == 0, (r.stdout or r.stderr).strip().replace("\n", " ")[:200]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dev", type=int, default=0, help="adapter number (hci0 = 0)")
    args = ap.parse_args()

    for attempt in range(10):          # the adapter may still be coming up at boot
        try:
            ok, msg = send_raw(args.dev)
            break
        except OSError as e:
            ok, msg = False, f"raw HCI socket failed: {e}"
            time.sleep(1)
    if not ok:
        ok2, msg2 = send_hcitool(args.dev)
        if ok2:
            ok, msg = True, f"via hcitool ({msg2})"
        else:
            msg = f"{msg}; hcitool: {msg2}"
    print(f"SCO routing over HCI on hci{args.dev}: {'OK' if ok else 'FAILED'} - {msg}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
