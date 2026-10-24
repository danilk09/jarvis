"""
A text-only Jarvis client, for testing the turn API (Phase 3, M1) without a phone or mic.

    python scripts/pi/fake_client.py https://jarvis-pi.<tailnet>.ts.net:5151
    python scripts/pi/fake_client.py http://localhost:5151 --source phone

Type a command and press Enter: it goes to POST /api/turn, and everything Jarvis does comes
back on GET /api/client/events and is printed (lines, state, "waiting for your answer").
When Jarvis asks something, just type the answer. --say saves each line's audio to say/.
Standard library only.
"""

import argparse
import json
import os
import sys
import threading
import urllib.request


def events(base, say_dir):
    while True:
        try:
            with urllib.request.urlopen(base + "/api/client/events", timeout=60) as r:
                for raw in r:
                    raw = raw.decode("utf-8", "replace").strip()
                    if raw.startswith("data: "):
                        show(json.loads(raw[6:]), base, say_dir)
        except Exception as e:
            print(f"\n  [events] reconnecting ({e})")


def show(ev, base, say_dir):
    t = ev.get("type")
    if t == "hello":
        print(f"  [connected] state={ev['state']}" + (f", waiting for: {ev['listen']['question']}" if ev.get("listen") else ""))
    elif t == "line":
        print(f"\n  JARVIS ({ev.get('source') or 'own'}): {ev['text']}")
        if say_dir:
            os.makedirs(say_dir, exist_ok=True)
            try:
                with urllib.request.urlopen(f"{base}/api/say/{ev['id']}", timeout=60) as r:
                    path = os.path.join(say_dir, f"{ev['id']}.wav")
                    with open(path, "wb") as f:
                        f.write(r.read())
                    print(f"     audio: {path}")
            except Exception as e:
                print(f"     audio failed: {e}")
    elif t == "user":
        print(f"  you ({ev.get('source')}): {ev['text']}")
    elif t == "state":
        print(f"  [{ev['state']}]")
    elif t == "listen":
        print(f"  [waiting for your answer{': ' + ev['question'] if ev.get('question') else ''}]")
    elif t == "listen_end":
        print("  [stopped waiting]")
    print("> ", end="", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="Jarvis's address, e.g. https://jarvis-pi.tail1234.ts.net:5151")
    ap.add_argument("--source", default="phone", help="what the turns say they came from")
    ap.add_argument("--say", action="store_true", help="save each line's speech to say/")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    threading.Thread(target=events, args=(base, "say" if a.say else ""), daemon=True).start()
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        req = urllib.request.Request(base + "/api/turn", data=json.dumps({"text": text, "source": a.source}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"  send failed: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
