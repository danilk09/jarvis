#!/usr/bin/env python3
"""
Offline self-test for bench.py: no Bluetooth, PipeWire or AirPods needed, runs on any OS.

    ./scripts/pocket/bench selftest        (or: python3 scripts/pocket/bench_selftest.py)

Feeds sample `pactl` / `btmon` output and fake evdev devices through the parsers and checks
profile picking, the button trigger, word error rate, WAV writing, the mic verdict, the
/status reply matching, and the report + verdict. Prints PASS/FAIL per check.
"""

import builtins
import os
import sys
import tempfile
import types
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"

_results = []


def check(name, cond, detail=""):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  ({detail})"), flush=True)


def section(title):
    print(f"\n{title}", flush=True)


# ── sample pactl output ───────────────────────────────────────────────────────

def card_text(profiles, active, mac=MAC, other_card=True):
    """`pactl list cards` as PipeWire's pulse server prints it. profiles: [(name, desc, available)]."""
    lines = []
    if other_card:
        lines += ["Card #42",
                  "\tName: alsa_card.platform-107c701400.hdmi",
                  "\tDriver: alsa",
                  "\tProfiles:",
                  "\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)",
                  "\t\toutput:hdmi-stereo: Digital Stereo (HDMI) Output (sinks: 1, sources: 0, priority: 5900, available: yes)",
                  "\tActive Profile: output:hdmi-stereo",
                  ""]
    lines += ["Card #57",
              f"\tName: bluez_card.{mac.replace(':', '_')}",
              "\tDriver: module-bluez5-device.c",
              "\tOwner Module: n/a",
              "\tProperties:",
              f"\t\tapi.bluez5.address = \"{mac}\"",
              "\t\tdevice.description = \"AirPods Pro\"",
              "\tProfiles:",
              "\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)"]
    for name, desc, avail in profiles:
        io = "sinks: 1, sources: 1" if name.startswith("headset") else "sinks: 1, sources: 0"
        lines.append(f"\t\t{name}: {desc} ({io}, priority: 1, available: {'yes' if avail else 'no'})")
    lines += [f"\tActive Profile: {active}",
              "\tPorts:",
              "\t\theadset-output: Headset (type: Headset, priority: 0, latency offset: 0 usec, availability unknown)",
              "\t\t\tPart of profile(s): a2dp-sink, headset-head-unit"]
    return "\n".join(lines) + "\n"


A2DP = [("a2dp-sink-sbc_xq", "High Fidelity Playback (A2DP Sink, codec SBC-XQ)", True),
        ("a2dp-sink", "High Fidelity Playback (A2DP Sink, codec SBC)", True)]

# PipeWire 1.x: plain = best codec (mSBC), -cvsd suffix for the fallback
CARD_NEW = card_text(A2DP + [
    ("headset-head-unit-cvsd", "Headset Head Unit (HSP/HFP, codec CVSD)", True),
    ("headset-head-unit", "Headset Head Unit (HSP/HFP, codec mSBC)", True)], "a2dp-sink")
# the other naming: -msbc suffix, plain = CVSD
CARD_MSBC_SUFFIX = card_text(A2DP + [
    ("headset-head-unit-msbc", "Headset Head Unit (HSP/HFP, codec mSBC)", True),
    ("headset-head-unit", "Headset Head Unit (HSP/HFP, codec CVSD)", True)], "a2dp-sink")
# older PipeWire: no codec in the description
CARD_NO_DESC = card_text(A2DP + [
    ("headset-head-unit-cvsd", "Headset Head Unit (HSP/HFP)", True),
    ("headset-head-unit", "Headset Head Unit (HSP/HFP)", True)], "a2dp-sink")
# HFP not connected yet
CARD_NO_HFP = card_text(A2DP + [
    ("headset-head-unit", "Headset Head Unit (HSP/HFP, codec mSBC)", False)], "a2dp-sink")
# only one unnamed headset profile (CVSD-only headset)
CARD_ONLY_PLAIN = card_text(A2DP + [("headset-head-unit", "Headset Head Unit (HSP/HFP)", True)], "a2dp-sink")

SINKS = ("41\talsa_output.platform-107c701400.hdmi.hdmi-stereo\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
         f"63\tbluez_output.{MAC.replace(':', '_')}.1\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n")
SOURCES = ("41\talsa_output.platform-107c701400.hdmi.hdmi-stereo.monitor\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
           f"63\tbluez_output.{MAC.replace(':', '_')}.1.monitor\tPipeWire\ts16le 1ch 16000Hz\tSUSPENDED\n"
           f"64\tbluez_input.{MAC.replace(':', '_')}.0\tPipeWire\ts16le 1ch 16000Hz\tSUSPENDED\n")


def test_pactl():
    section("pactl parsing and profile picking")
    card = bench.find_card(MAC, CARD_NEW)
    check("finds the AirPods card among others", card and card["name"] == "bluez_card.AA_BB_CC_DD_EE_FF",
          card)
    names = [p["name"] for p in card["profiles"]]
    check("lists every profile, not the Ports/Properties lines",
          names == ["off", "a2dp-sink-sbc_xq", "a2dp-sink", "headset-head-unit-cvsd", "headset-head-unit"], names)
    check("reads the active profile", card["active"] == "a2dp-sink", card["active"])
    check("other MAC -> None", bench.find_card("11:22:33:44:55:66", CARD_NEW) is None)
    check("a2dp prefers plain a2dp-sink over sbc_xq", bench.pick_profile(card, "a2dp") == "a2dp-sink")

    cases = [("1.x naming (plain = mSBC)", CARD_NEW, "headset-head-unit", "headset-head-unit-cvsd"),
             ("-msbc suffix naming (plain = CVSD)", CARD_MSBC_SUFFIX, "headset-head-unit-msbc", "headset-head-unit"),
             ("no codec in description", CARD_NO_DESC, "headset-head-unit", "headset-head-unit-cvsd"),
             ("one plain profile only", CARD_ONLY_PLAIN, None, "headset-head-unit")]
    for label, text, want_msbc, want_cvsd in cases:
        c = bench.find_card(MAC, text)
        got = (bench.pick_profile(c, "msbc"), bench.pick_profile(c, "cvsd"))
        check(f"msbc/cvsd picks: {label}", got == (want_msbc, want_cvsd), got)
    c = bench.find_card(MAC, CARD_NO_HFP)
    check("unavailable headset profile is ignored",
          bench.pick_profile(c, "hfp") is None and c["profiles"][-1]["available"] is False)
    check("hfp falls back to whatever headset profile exists",
          bench.pick_profile(bench.find_card(MAC, CARD_ONLY_PLAIN), "hfp") == "headset-head-unit")
    check("no card -> no profile", bench.pick_profile(None, "a2dp") is None and bench.pick_profile(None, "hfp") is None)

    check("sinks: only the AirPods' one", bench.node_names("sinks", MAC, SINKS) == ["bluez_output.AA_BB_CC_DD_EE_FF.1"])
    check("sources: monitors skipped", bench.node_names("sources", MAC, SOURCES) == ["bluez_input.AA_BB_CC_DD_EE_FF.0"])


def test_set_profile():
    """Switching back to music mode must wait for the headset nodes to go, not return at once
    because the old headset sink is still listed."""
    section("profile switching (simulated pactl)")
    key = MAC.replace(":", "_")
    st = {"active": "headset-head-unit", "polls": 0}
    hfp_nodes = (f"63\tbluez_output.{key}.1\tPipeWire\ts16le 1ch 16000Hz\tRUNNING\n",
                 f"64\tbluez_input.{key}.0\tPipeWire\ts16le 1ch 16000Hz\tRUNNING\n")
    a2dp_nodes = (f"65\tbluez_output.{key}.1\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n", "")

    def fake_run(cmd, timeout=20, sudo=False, input_text=None):
        out = ""
        if cmd[:2] == ["pactl", "set-card-profile"]:
            st["target"], st["polls"] = cmd[3], 0
        elif cmd[:3] == ["pactl", "list", "cards"]:
            st["polls"] += 1
            active = st["target"] if st["polls"] > 2 else st["active"]   # card flips after a few polls
            out = card_text(A2DP + [("headset-head-unit", "Headset Head Unit (HSP/HFP, codec mSBC)", True)], active)
        elif cmd[:3] == ["pactl", "list", "short"]:
            st["polls"] += 1
            nodes = a2dp_nodes if st["polls"] > 4 else hfp_nodes          # old nodes linger a little longer
            out = nodes[0] if cmd[3] == "sinks" else nodes[1]
        return bench.subprocess.CompletedProcess(cmd, 0, out, "")

    real_run, real_sleep = bench.run, bench.time.sleep
    bench.run, bench.time.sleep = fake_run, (lambda s: None)
    try:
        secs = bench.set_profile(MAC, "a2dp-sink", want_source=False, timeout=5)
        check("waits until the headset mic is gone and the card says a2dp-sink", secs is not None and st["polls"] > 4,
              f"secs={secs} polls={st['polls']}")
        st.update(active="a2dp-sink")
        hfp_nodes, a2dp_nodes = a2dp_nodes, hfp_nodes    # now going the other way
        secs = bench.set_profile(MAC, "headset-head-unit", want_source=True, timeout=5)
        check("headset mode is ready once the mic source exists", secs is not None and st["polls"] > 4,
              f"secs={secs} polls={st['polls']}")
    finally:
        bench.run, bench.time.sleep = real_run, real_sleep


# ── sample btmon output ───────────────────────────────────────────────────────

def hex_lines(data, indent=8):
    """btmon's hex dump: 16 bytes per line, hex column padded to 48, then ASCII."""
    out = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = "".join(f"{b:02x} " for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(" " * indent + f"{hx:<48}  {asc}\n")
    return out


def acl(direction, payload, n):
    rx = direction == ">"
    hdr = [f"{direction} ACL Data {'RX' if rx else 'TX'}: Handle 11 flags 0x02 dlen {len(payload) + 8}"
           f"                #{n} [hci0] 12.{n:06d}\n",
           f"      Channel: {'64' if rx else '65'} len {len(payload) + 4} [PSM 3 mode Basic (0x00)] {{chan 2}}\n",
           "      RFCOMM: Unnumbered Info with Header Check (UIH) (0xef)\n",
           "         Address: 0x0b cr 1 dlci 0x02\n",
           "         Control: 0xef poll/final 0\n",
           f"         Length: {len(payload)}\n",
           "         FCS: 0x9a\n"]
    return hdr + hex_lines(payload)


def avrcp(op_hex, name, state, n):
    return [f"> ACL Data RX: Handle 11 flags 0x02 dlen 12                #{n} [hci0] 13.{n:06d}\n",
            "      Channel: 66 len 8 [PSM 23 mode Basic (0x00)] {chan 3}\n",
            "      AVCTP Control: Command: type 0x00 label 3 PID 0x110e\n",
            "        AV/C: Control: address 0x48 opcode 0x7c\n",
            "          Subunit: Panel\n",
            "          Opcode: Passthrough\n",
            f"          Operation: {op_hex} ({name} {state})\n",
            "          Length: 0x00\n"]


# A real-looking capture: the SLC handshake (HF features, the Pi's +BRSF reply split over two
# dump lines, status chatter), then a press-and-hold (AT+BVRA=1), then a PLAY passthrough.
BTMON_HANDSHAKE = (
    ["Bluetooth monitor ver 5.79\n",
     "= Note: Linux version 6.12.25+rpt-rpi-2712 (aarch64)                       0.000000\n",
     "@ MGMT Event: Device Connected (0x000b) plen 13              {0x0001} [hci0] 11.900000\n"]
    + acl(">", b"AT+BRSF=959\r", 1)
    + acl("<", b"\r\n+BRSF: 1636\r\n\r\nOK\r\n", 2)      # 22 bytes -> two dump lines
    + acl(">", b"AT+BAC=1,2\r", 3)
    + acl(">", b"AT+CIND=?\r", 4)
    + acl(">", b"AT+XAPL=05AC-200E-0100,10\r", 5)
)
BTMON_HOLD = acl(">", b"AT+CLCC\r", 6) + acl(">", b"AT+BVRA=1\r", 7) + acl("<", b"\r\nOK\r\n", 8)
BTMON_SINGLE = avrcp("0x44", "PLAY", "Pressed", 9) + avrcp("0x44", "PLAY", "Released", 10)
BTMON_DOUBLE = avrcp("0x4b", "FORWARD", "Pressed", 11) + avrcp("0x4b", "FORWARD", "Released", 12)


def parse_btmon(lines):
    w = bench.BtmonWatcher(os.devnull)
    w.consume(lines)
    return w


def test_btmon():
    section("btmon parsing (AT commands from hex dumps, AVRCP)")
    w = parse_btmon(BTMON_HANDSHAKE)
    ats = [d for _, k, d in w.since(0) if k == "at"]
    check("HF features AT+BRSF=959", "AT+BRSF=959" in ats, ats)
    check("+BRSF reply split across two dump lines is found", "+BRSF: 1636" in ats, ats)
    check("vendor command AT+XAPL with dashes parsed", any(a.startswith("AT+XAPL=05AC") for a in ats), ats)
    check("handshake is all chatter (summarize drops it)", bench.summarize(w.since(0)) == [],
          bench.summarize(w.since(0)))

    hold = bench.summarize(parse_btmon(BTMON_HOLD).since(0))
    check("hold: AT+BVRA=1 kept, AT+CLCC polling dropped", hold == ["at:AT+BVRA=1"], hold)
    single = bench.summarize(parse_btmon(BTMON_SINGLE).since(0))
    check("single: AVRCP PLAY pressed, released dropped", single == ["avrcp:PLAY pressed"], single)
    double = bench.summarize(parse_btmon(BTMON_DOUBLE).since(0))
    check("double: AVRCP FORWARD pressed", double == ["avrcp:FORWARD pressed"], double)

    hsp = bench.summarize(parse_btmon(acl(">", b"AT+CKPD=200\r", 1) + acl(">", b"ATA\r", 2)).since(0))
    check("HSP button (AT+CKPD=200) and answer (ATA) are seen", hsp == ["at:AT+CKPD=200", "at:ATA"], hsp)
    noise = bench.summarize(parse_btmon(["        41 54 2b 42 56 52 41 3d 31 0d" + " " * 20 + "AT+BVRA=1.\n"]).since(0))
    check("hex dump without a packet header still parsed at the end", noise == ["at:AT+BVRA=1"], noise)

    feats = None
    for d in ats:
        m = bench.re.match(r"\+BRSF:\s*(\d+)", d)
        if m:
            feats = int(m.group(1))
    check("+BRSF 1636 has bit 2 (voice recognition offered)", feats == 1636 and bool(feats & 0x4))
    check("+BRSF 1632 does not", not (1632 & 0x4))


class _FakeDev:
    def __init__(self, path, name):
        self.path, self.name = path, name


def test_evdev():
    section("evdev (fake devices)")
    ecodes = types.SimpleNamespace(KEY={164: "KEY_PLAYPAUSE", 163: "KEY_NEXTSONG", 200: ["KEY_PLAYCD", "KEY_PLAY"]},
                                   EV_KEY=1)
    check("key name", bench.key_name(ecodes, 164) == "KEY_PLAYPAUSE")
    check("alias list -> first name", bench.key_name(ecodes, 200) == "KEY_PLAYCD")
    check("unknown code -> number", bench.key_name(ecodes, 999) == "999")

    devs = {"/dev/input/event0": "vc4-hdmi-0", "/dev/input/event1": "AirPods Pro (AVRCP)",
            "/dev/input/event2": "pwr_button", "/dev/input/event3": "denied"}

    def input_device(p):
        if devs[p] == "denied":
            raise PermissionError(p)
        return _FakeDev(p, devs[p])

    w = bench.EvdevWatcher("AirPods Pro")
    w.evdev = types.SimpleNamespace(list_devices=lambda: list(devs), InputDevice=input_device, ecodes=ecodes)
    found = [d.name for d in w._scan()]
    check("finds only BlueZ's AVRCP device", found == ["AirPods Pro (AVRCP)"], found)
    check("permission problem is reported", w.error and "permission" in w.error, w.error)

    w.events = [(10.0, "key", "KEY_NEXTSONG"), (10.1, "key", "KEY_NEXTSONG")]
    check("evdev keys summarized once", bench.summarize(w.since(0)) == ["key:KEY_NEXTSONG"])


PLAY = ["avrcp:PLAY pressed", "key:KEY_PLAYCD"]


def test_trigger():
    section("trigger choice")
    aap = {"left_single": ["aap:left single"], "left_double": ["aap:left double"], "left_triple": ["aap:left triple"],
           "left_hold": ["aap:left long"], "right_single": ["aap:right single"], "right_double": ["aap:right double"],
           "right_hold": ["aap:right long"]}
    got = bench.pick_trigger(aap)
    check("AAP left/right -> left earbud trigger with hold", got["trigger"] == "left" and got["left_long"]
          and got["right_presses"] == ["double", "long", "single"], got)
    no_hold = dict(aap, left_hold=[])
    got = bench.pick_trigger(no_hold)
    check("left trigger without press-and-hold", got["trigger"] == "left" and not got["left_long"], got)
    # what the Pi 5 run on 2026-10-19 actually saw: every press = PLAY, hold nothing
    pi5 = {"left_single": PLAY, "left_double": PLAY, "left_triple": PLAY, "left_hold": [],
           "right_single": PLAY, "right_double": PLAY, "right_hold": []}
    got = bench.pick_trigger(pi5)
    check("all presses PLAY (the Pi 5 run) -> any press", got["trigger"] == "press" and got["match"] == sorted(PLAY), got)
    got = bench.pick_trigger({"left_hold": ["at:AT+BVRA=1"], "left_double": ["avrcp:FORWARD pressed"]})
    check("hold sends BVRA -> hold", got == {"trigger": "hold", "match": ["at:AT+BVRA=1"]}, got)
    got = bench.pick_trigger({"left_single": PLAY, "left_double": ["key:KEY_NEXTSONG"]})
    check("double sends next track -> double", got == {"trigger": "double", "match": ["key:KEY_NEXTSONG"]}, got)
    got = bench.pick_trigger({"left_single": ["aap:left single"], "right_single": []})
    check("AAP presses but no right ones -> any press (aap prefix)", got == {"trigger": "press", "match": ["aap:"]}, got)
    check("nothing -> none", bench.pick_trigger({k: [] for k, _ in bench.GESTURES})["trigger"] == "none")

    check("hit: left prefix matches a left press", bench.trigger_hit(["aap:left "], ["aap:left double"]) == "aap:left double")
    check("hit: right press doesn't start Jarvis", bench.trigger_hit(["aap:left "], ["aap:right single"]) is None)
    check("hit: exact label", bench.trigger_hit(sorted(PLAY), ["key:KEY_PLAYCD"]) == "key:KEY_PLAYCD")
    check("hit: exact label doesn't prefix-match", bench.trigger_hit(["key:KEY_PLAY"], ["key:KEY_PLAYCD"]) is None)


def test_aap():
    section("Apple accessory protocol (AAP) packets")
    A = bench.AapWatcher
    check("handshake bytes", A.HANDSHAKE.hex(" ") == "00 00 04 00 01 00 02 00 00 00 00 00 00 00 00 00")
    check("notification request", A.NOTIFY.hex(" ") == "04 00 04 00 0f 00 ff ff ff ff")
    check("stem config: all presses to us", A.stem_config(0x0F).hex(" ") == "04 00 04 00 09 00 39 0f 00 00 00")
    check("stem config: give them back", A.stem_config(0x00).hex(" ") == "04 00 04 00 09 00 39 00 00 00 00")
    cases = [("04 00 04 00 19 00 05 01", "left single"), ("04 00 04 00 19 00 06 02", "right double"),
             ("04 00 04 00 19 00 07 01", "left triple"), ("04 00 04 00 19 00 08 02", "right long")]
    for hx, want in cases:
        got = A.parse(bytes.fromhex(hx))
        check(f"stem press {hx[-5:]} -> {want}", got == ("press", want), got)
    check("unknown press values still reported", A.parse(bytes.fromhex("04000400190009 03".replace(" ", "")))
          == ("press", "bud03 type09"))
    check("battery packet ignored", A.parse(bytes.fromhex("040004000400030201640201040163010108011102 01".replace(" ", ""))) is None)
    check("not AAP -> None", A.parse(b"\x00\x01") is None and A.parse(bytes.fromhex("0500040019000501")) is None)
    # metadata example from LibrePods' docs (AirPods Pro, model A3048), trimmed after the firmware
    meta = bytes.fromhex("040004001d0002d5000400416972506f64732050726f004133303438004170706c6520496e632e00"
                         "51584e524848595850360036312e313836383034303030323030303030302e3237313300")
    kind, info = A.parse(meta)
    check("metadata -> model and firmware", kind == "info" and info.get("model") == "A3048"
          and info.get("firmware", "").startswith("61.1868"), info)
    check("metadata keeps the serial number out", "QXNRHHYXP6" not in str(info))


def test_bluetoothctl():
    section("interactive bluetoothctl output")
    raw = "\x1b[0;94m[bluetooth]\x1b[0m# \x1b[0;92m[NEW]\x1b[0m Device F0:04:E1:C7:A7:2F DanilのAirPods Pro"
    line = bench.bt_line(raw)
    check("colours and prompt stripped", line == "[NEW] Device F0:04:E1:C7:A7:2F DanilのAirPods Pro", repr(line))
    check("new device parsed (non-ASCII name)", bench.bt_device(line) == ("F0:04:E1:C7:A7:2F", "DanilのAirPods Pro"))
    check("unresolved name kept", bench.bt_device("[NEW] Device 11:22:33:44:55:66 11-22-33-44-55-66")
          == ("11:22:33:44:55:66", "11-22-33-44-55-66"))
    check("name change parsed", bench.bt_device("[CHG] Device 11:22:33:44:55:66 Name: AirPods Pro")
          == ("11:22:33:44:55:66", "AirPods Pro"))
    check("RSSI change ignored", bench.bt_device("[CHG] Device 11:22:33:44:55:66 RSSI: -61") is None)
    check("property line ignored", bench.bt_device("Device 11:22:33:44:55:66 RSSI: -61") is None)
    check("other lines ignored", bench.bt_device("Pairing successful") is None)
    check("prompt with device name stripped", bench.bt_line("[DanilのAirPods Pro]# Pairing successful")
          == "Pairing successful")


def test_wer():
    section("word error rate")
    ref = bench.TEST_SENTENCE
    check("identical -> 0", bench.wer(ref, ref) == 0.0)
    check("case, punctuation and curly apostrophe ignored",
          bench.wer(ref, "jarvis what’s the weather going to be like in honolulu tomorrow afternoon") == 0.0)
    check("one substitution in 12 words -> 0.08",
          bench.wer(ref, "Jarvis, what's the weather going to be like in Honolulu tomorrow morning?") == 0.08)
    check("one deletion", bench.wer("a b c d", "a b d") == 0.25)
    check("one insertion", bench.wer("a b c d", "a b x c d") == 0.25)
    check("empty transcript -> 1.0", bench.wer(ref, "") == 1.0)
    check("empty reference doesn't divide by zero", bench.wer("", "hello") == 1.0)


def test_wav(tmp):
    section("WAV writing and level stats")
    p = os.path.join(tmp, "t.wav")
    bench.write_wav(p, b"\x00\x00" * 1600)
    with wave.open(p) as w:
        check("16 kHz mono 16-bit", (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2))
        check("frame count", w.getnframes() == 1600)
    chime = bench.tone_wav(os.path.join(tmp, "chime.wav"), [(880, 0.12), (0, 0.05), (1320, 0.18)])
    with wave.open(chime) as w:
        pcm = w.readframes(w.getnframes())
        check("chime length 0.35 s", abs(w.getnframes() / 16000 - 0.35) < 0.01, w.getnframes())
    st = bench.pcm_stats(pcm)
    check("chime is loud, not silent", st["rms_dbfs"] > -20 and 0 < st["peak"] <= 32767 and st["zero_frac"] < 0.5, st)
    check("silence detected", bench.pcm_stats(b"\x00\x00" * 16000)["zero_frac"] == 1.0)
    check("empty recording", bench.pcm_stats(b"")["seconds"] == 0 and bench.pcm_stats(b"")["peak"] == 0)
    check("frame_rms of a constant", round(bench.frame_rms(bench.struct.pack("<4h", 100, -100, 100, -100))) == 100)


def test_mic_verdict():
    section("mic verdict")
    msbc = {"codec": "msbc", "ok": True, "wer": 0.08, "rating": 4}
    cvsd = {"codec": "cvsd", "ok": True, "wer": 0.23, "rating": 3}
    check("lowest word error wins", bench.best_mic([cvsd, msbc])["codec"] == "msbc")
    check("no Whisper + rated 1 -> not usable", bench.best_mic([{"codec": "msbc", "ok": True, "rating": 1}]) is None)
    check("no Whisper + rated 4 -> usable", bench.best_mic([{"codec": "msbc", "ok": True, "rating": 4}]) is not None)
    check("silent run never wins", bench.best_mic([{"codec": "msbc", "ok": False, "silent": True}]) is None)
    check("garbled but rated OK still counts", bench.best_mic([{"codec": "cvsd", "ok": True, "wer": 0.6,
                                                                "rating": 3}])["codec"] == "cvsd")


def test_ask_int():
    section("typed answers")
    real = builtins.input
    try:
        for typed, want in (("4", 4), ("4/5", 4), ("", 3), ("great", 3), ("9", 5), ("0", 1)):
            builtins.input = lambda _p, t=typed: t
            got = bench.ask_int("rate?", 3, 1, 5)
            check(f"rating {typed!r} -> {want}", got == want, got)
    finally:
        builtins.input = real


def test_status_log():
    section("/status reply matching")
    before = [{"role": "user", "text": "what time is it", "time": "10:00:01", "sample": "s1"},
              {"role": "jarvis", "text": "It's 10 o'clock.", "time": "10:00:02"}]
    after = before + [{"role": "user", "text": "what's on my agenda", "time": "10:01:00"},
                      {"role": "jarvis", "text": "Two things today.", "time": "10:01:02"}]
    new = bench.new_entries(before, after)
    check("only the new entries", [e["text"] for e in new] == ["what's on my agenda", "Two things today."],
          new)
    corrected = [dict(before[0], text="what time is it?", corrected=True)] + after[1:]
    new = [e["text"] for e in bench.new_entries(before, corrected) if e["role"] == "jarvis"]
    check("an old line corrected in place doesn't bring old replies back", new == ["Two things today."], new)
    rolled = after[2:]          # the log only keeps the last 40
    new = [e["text"] for e in bench.new_entries(before, rolled) if e["role"] == "jarvis"]
    check("old entries rolled off the log", new == ["Two things today."], new)
    check("empty before -> everything", len(bench.new_entries([], after)) == 4)


def test_report(tmp):
    section("report and verdict")
    real = bench.RESULTS_DIR, bench.REPORT_FILE
    bench.RESULTS_DIR, bench.REPORT_FILE = tmp, os.path.join(tmp, "report.md")
    try:
        base = {"config": {"mac": MAC, "name": "AirPods Pro"},
                "check": {"model": "Raspberry Pi 5 Model B Rev 1.0"},
                "pair": {"ok": True, "connect_s": 3.1, "profiles": ["a2dp-sink", "headset-head-unit"]},
                "sound": {"ok": True}}
        mic_ok = {"ok": True, "best": "msbc", "back_to_music_s": 0.8,
                  "runs": [{"codec": "msbc", "ok": True, "rms_dbfs": -28.4, "switch_s": 1.2, "first_sound_s": 0.6,
                            "rating": 4, "transcript": "Jarvis what's the weather", "wer": 0.08},
                           {"codec": "cvsd", "ok": False, "why": "profile not offered"}]}
        cases = [
            ("nothing run", {}, "INCOMPLETE"),
            ("music failed", dict(base, sound={"ok": False}), "INCOMPLETE"),
            ("mic not run", base, "INCOMPLETE"),
            ("mic silent", dict(base, mic={"ok": False, "runs": [{"codec": "msbc", "ok": False, "silent": True,
                                                                  "why": "recording is silence"}]}),
             "NO-GO (AirPods mic)"),
            ("buttons not run", dict(base, mic=mic_ok), "LIKELY GO"),
            ("hold works", dict(base, mic=mic_ok, buttons={"trigger": "hold", "seen": {"hold": ["at:AT+BVRA=1"]},
                                                           "ag_features": 1636, "ag_offers_voice_recognition": True}),
             "GO"),
            ("double only", dict(base, mic=mic_ok, buttons={"trigger": "double", "seen": {}}),
             "GO (double-press trigger)"),
            ("left/right via AAP", dict(base, mic=mic_ok, buttons={"trigger": "left", "left_long": True, "seen": {}}),
             "GO (left earbud = Jarvis)"),
            ("all presses alike", dict(base, mic=mic_ok, buttons={"trigger": "press", "seen": {}}),
             "GO (any-press trigger)"),
            ("no press", dict(base, mic=mic_ok, buttons={"trigger": "none", "seen": {}}),
             "GO (needs another trigger)"),
        ]
        for label, res, want in cases:
            v, why = bench.verdict(res)
            check(f"verdict: {label} -> {want}", v == want and why, v)

        res = dict(base, mic=mic_ok, updated="2026-10-14 10:00:00",
                   buttons={"trigger": "left", "match": ["aap:left "], "left_long": True,
                            "seen": {"left_single": ["aap:left single"], "left_hold": ["aap:left long"],
                                     "right_double": ["aap:right double"]},
                            "aap": True, "aap_info": {"model": "A3048", "firmware": "7A305"},
                            "ag_features": 1632, "ag_offers_voice_recognition": False},
                   jarvis={"successful": 2, "turns": [{}, {}, {}], "avg_press_to_listening_s": 1.4,
                           "avg_speech_end_to_voice_s": 3.2})
        v, _ = bench.write_report(res)
        with open(bench.REPORT_FILE, encoding="utf-8") as f:
            text = f.read()
        check("report written with the verdict", v == "GO (left earbud = Jarvis)"
              and "**Verdict: GO (left earbud = Jarvis)**" in text)
        for frag in ("| Press: left hold | aap:left long |", "| Press: right double | aap:right double |",
                     "**left** (any press on the LEFT earbud)", "| Apple protocol (AAP) | connected · model A3048",
                     "| Mic MSBC | OK", "word error 8%", "| Mic CVSD | not offered |",
                     "2 of 3", "1632 (voice recognition offered: False)", "Raspberry Pi 5", "<details>"):
            check(f"report contains {frag!r}", frag in text)
        check("MAC address kept out of the raw results", MAC not in text)
    finally:
        bench.RESULTS_DIR, bench.REPORT_FILE = real


def selftest():
    print("AirPods bench self-test (offline)")
    with tempfile.TemporaryDirectory() as tmp:
        for t in (test_pactl, test_set_profile, test_btmon, test_evdev, test_trigger, test_aap, test_bluetoothctl,
                  test_wer,
                  lambda: test_wav(tmp), test_mic_verdict, test_ask_int, test_status_log, lambda: test_report(tmp)):
            try:
                t()
            except Exception as e:          # a crash is a failure, keep going
                import traceback
                traceback.print_exc()
                check(f"{getattr(t, '__name__', 'test')} crashed", False, e)
    passed, total = sum(_results), len(_results)
    print(f"\n{passed}/{total} checks passed - {'PASS' if passed == total else 'FAIL'}")
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if selftest() else 1)
