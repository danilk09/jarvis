#!/usr/bin/env python3
"""
Jarvis's power switch on the Pi: a tiny always-on web page (port 5152) with Start / Stop
buttons, so Jarvis only runs when you want it to. Standard library only, ~0% CPU.

    python3 scripts/pi/control.py serve      the switch itself (jarvis-control.service runs it)
    python3 scripts/pi/control.py start      start Jarvis      (also: stop, status)
    python3 scripts/pi/control.py nightly    the 11 PM shutdown (jarvis-nightly.timer runs it)
    python3 scripts/pi/control.py link       print the phone link (with its key) again

- Jarvis (jarvis.service) isn't started at boot by systemd: the switch remembers whether you
  last left it on or off (jarvis_memory/control.json) and restores that after a reboot.
- 11 PM: if Jarvis is running, it's stopped and your phone gets a notification with a
  "Start Jarvis" button (ntfy, same topic as Jarvis's reminders).
- Heat guard: if the CPU stays at HOT_C or above for HOT_MINUTES minutes, Jarvis is stopped and
  your phone is told. (The Pi slows itself down at ~80-85 C anyway; this is the backstop.)
- Every call needs the key from control.json; the link printed by `link` carries it once and the
  page remembers it. Served over HTTPS with Jarvis's Tailscale certificate when there is one.
Starting/stopping uses `sudo -n systemctl start|stop jarvis.service`, allowed without a password
by /etc/sudoers.d/jarvis-control (install_service.sh writes it).
"""

import ast
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import pihealth  # noqa: E402

PORT = 5152
JARVIS_PORT = 5151
UNIT = "jarvis.service"
STATE_FILE = os.path.join(ROOT, "jarvis_memory", "control.json")
NOTIFY_FILE = os.path.join(ROOT, "jarvis_memory", "notify.json")
SYSTEMCTL = "/usr/bin/systemctl" if os.path.exists("/usr/bin/systemctl") else "systemctl"
HOT_C = 80.0
HOT_MINUTES = 3
NIGHTLY = "11:00 PM"          # shown on the page; the time itself is in jarvis-nightly.timer

_lock = threading.Lock()


# ── state ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if not st.get("key"):
        st.setdefault("desired", "on")
        st["key"] = secrets.token_urlsafe(18)
        save_state(st)
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


def log(msg):
    print(f"[control] {msg}", flush=True)


# ── Jarvis on / off ───────────────────────────────────────────────────────────

def jarvis_state():
    return (pihealth.service(UNIT) or {}).get("state", "unknown")


def set_jarvis(on, why):
    """Start or stop Jarvis and remember it. -> (ok, message)"""
    with _lock:
        st = load_state()
        st["desired"] = "on" if on else "off"
        st["changed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        st["why"] = why
        save_state(st)
        r = subprocess.run(["sudo", "-n", SYSTEMCTL, "start" if on else "stop", UNIT],
                           capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip() or f"systemctl exited {r.returncode}"
        if "password" in msg.lower():
            msg += " (sudo rule missing: run bash scripts/pi/install_service.sh)"
        log(f"{'start' if on else 'stop'} failed: {msg}")
        return False, msg
    log(f"Jarvis {'started' if on else 'stopped'} ({why})")
    return True, "started" if on else "stopped"


def status():
    st = load_state()
    svc = pihealth.service(UNIT) or {}
    return {"jarvis": svc.get("state", "unknown"), "since": svc.get("since"), "desired": st.get("desired"),
            "changed": st.get("changed"), "why": st.get("why"), "jarvis_mem_mb": svc.get("mem_mb"),
            "nightly_stop": NIGHTLY, "hot_limit_c": HOT_C, **pihealth.snapshot()}


# ── phone notifications (ntfy, Jarvis's topic) ────────────────────────────────

def _config_value(name, default):
    """A literal setting from core/config.py, without importing Jarvis."""
    try:
        with open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8") as f:
            for line in f:
                m = re.match(rf"^{name}\s*=\s*(.+?)\s*(#.*)?$", line)
                if m:
                    return ast.literal_eval(m.group(1))
    except (OSError, ValueError, SyntaxError):
        pass
    return default


def tailscale_name():
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout
        return json.loads(out).get("Self", {}).get("DNSName", "").rstrip(".") or None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def notify(title, message, start_button=False):
    if not _config_value("PHONE_PUSH", True):
        return
    try:
        with open(NOTIFY_FILE, encoding="utf-8") as f:
            topic = json.load(f).get("topic")
    except (OSError, ValueError):
        topic = None
    topic = _config_value("NTFY_TOPIC", "") or topic
    if not topic:
        log("no ntfy topic yet (Jarvis creates it on first start) - notification skipped")
        return
    body = {"topic": topic, "title": title, "message": message, "tags": ["robot"]}
    fqdn = tailscale_name()
    if start_button and fqdn:
        body["actions"] = [{"action": "http", "label": "Start Jarvis", "url": f"{base_url(fqdn)}/api/start",
                            "method": "POST", "headers": {"X-Key": load_state()["key"]}, "clear": True}]
    try:
        req = urllib.request.Request(_config_value("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as e:
        log(f"notification failed: {e}")


# ── HTTPS with Jarvis's Tailscale certificate ─────────────────────────────────

def cert_files(fqdn):
    if not fqdn:
        return None
    cert, key = os.path.join(ROOT, f"{fqdn}.crt"), os.path.join(ROOT, f"{fqdn}.key")
    if not (os.path.exists(cert) and os.path.exists(key)):
        # Jarvis normally fetches it; get it here too so the switch works before Jarvis ever ran
        subprocess.run(["tailscale", "cert", fqdn], cwd=ROOT, capture_output=True, timeout=120)
    return (cert, key) if os.path.exists(cert) and os.path.exists(key) else None


def base_url(fqdn):
    return f"{'https' if cert_files(fqdn) else 'http'}://{fqdn}:{PORT}"


# ── the page ──────────────────────────────────────────────────────────────────

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis Power</title>
<style>
 body{margin:0;background:#05080d;color:#cfe8ff;font-family:-apple-system,system-ui,sans-serif;
      display:flex;flex-direction:column;align-items:center;padding:28px 16px}
 h1{letter-spacing:.4em;font-weight:300;font-size:20px;margin:0 0 24px}
 #state{font-size:22px;margin:8px 0 4px}#since{color:#7c97b3;font-size:13px;min-height:16px}
 .row{display:flex;gap:14px;margin:26px 0}
 button{font-size:18px;padding:16px 28px;border-radius:14px;border:1px solid #2a5d8a;background:#0d1b2a;color:#cfe8ff}
 button:disabled{opacity:.35}#start{border-color:#2f8f5b}#stop{border-color:#a04545}
 table{border-collapse:collapse;font-size:14px;color:#9fb8d0}td{padding:4px 10px}td:first-child{color:#6f88a3}
 a{color:#6fb6ff}#msg{min-height:20px;margin:6px;color:#ffcf6f;font-size:14px;text-align:center}
 #keybox{display:none;margin:20px 0;text-align:center}input{font-size:15px;padding:8px;width:240px}
</style></head><body>
<h1>J A R V I S</h1>
<div id="state">...</div><div id="since"></div>
<div class="row"><button id="start" onclick="act('start')">Start</button>
<button id="stop" onclick="act('stop')">Stop</button></div>
<div id="msg"></div>
<table id="info"></table>
<p><a id="open" href="#">Open Jarvis</a></p>
<div id="keybox">Paste the key from <code>control.py link</code>:<br><input id="key"><br>
<button onclick="saveKey()">Save</button></div>
<script>
const q=new URLSearchParams(location.search);
if(q.get('key')){try{localStorage.setItem('jkey',q.get('key'))}catch(e){};history.replaceState(null,'',location.pathname)}
const key=()=>{try{return localStorage.getItem('jkey')||''}catch(e){return ''}};
document.getElementById('open').href=location.protocol+'//'+location.hostname+':JPORT/phone';
function saveKey(){try{localStorage.setItem('jkey',document.getElementById('key').value.trim())}catch(e){};refresh()}
const names={active:'Running',inactive:'Stopped',failed:'Stopped (crashed)',activating:'Starting...',
             deactivating:'Stopping...'};
async function call(path,method){
  const r=await fetch(path,{method:method||'GET',headers:{'X-Key':key()}});
  if(r.status==403){document.getElementById('keybox').style.display='block';throw new Error('key needed')}
  return r.json()}
async function refresh(){
  try{const s=await call('/api/status');document.getElementById('keybox').style.display='none';
    document.getElementById('state').textContent=names[s.jarvis]||s.jarvis;
    document.getElementById('since').textContent=s.jarvis=='active'&&s.since?'since '+s.since:
      (s.why?'last: '+s.why+(s.changed?' ('+s.changed+')':''):'');
    document.getElementById('start').disabled=s.jarvis=='active'||s.jarvis=='activating';
    document.getElementById('stop').disabled=!(s.jarvis=='active'||s.jarvis=='activating');
    const rows=[['Temperature',s.temp_c!=null?s.temp_c+' \\u00b0C':'?'],
      ['Fan',s.fan_rpm!=null?s.fan_rpm+' rpm':'?'],
      ['Memory free',s.mem_available_mb!=null?s.mem_available_mb+' of '+s.mem_total_mb+' MB':'?'],
      ['Jarvis uses',s.jarvis_mem_mb!=null?s.jarvis_mem_mb+' MB':'-'],
      ['Warnings',(s.throttle_flags||[]).join(', ')||'none'],
      ['Auto stop','every night at '+s.nightly_stop+', and above '+s.hot_limit_c+' \\u00b0C']];
    document.getElementById('info').innerHTML=rows.map(r=>'<tr><td>'+r[0]+'</td><td>'+r[1]+'</td></tr>').join('');
  }catch(e){document.getElementById('state').textContent=e.message=='key needed'?'Key needed':'Can\\'t reach the Pi'}}
async function act(what){
  document.getElementById('msg').textContent=what=='start'?'Starting (takes ~20 s)...':'Stopping...';
  try{const r=await call('/api/'+what,'POST');document.getElementById('msg').textContent=r.ok?'':r.message}
  catch(e){document.getElementById('msg').textContent=e.message}
  refresh()}
refresh();setInterval(refresh,4000);
</script></body></html>""".replace("JPORT", str(JARVIS_PORT))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return secrets.compare_digest(self.headers.get("X-Key", ""), load_state()["key"])

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/power"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, status()) if self._authorized() else self._send(403, {"error": "bad key"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/start", "/api/stop"):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(403, {"error": "bad key"})
        ok, msg = set_jarvis(path.endswith("start"), "phone button")
        self._send(200, {"ok": ok, "message": msg})


# ── background: boot state, heat guard, certificate renewal ───────────────────

def guard(cert):
    hot = 0
    cert_mtime = os.path.getmtime(cert[0]) if cert else None
    while True:
        time.sleep(60)
        t = pihealth.temp_c()
        hot = hot + 1 if t is not None and t >= HOT_C else 0
        if hot >= HOT_MINUTES and jarvis_state() == "active":
            set_jarvis(False, f"heat guard: {t} C for {HOT_MINUTES} min")
            notify("Jarvis stopped: Pi too hot", f"The Pi was at {t} C for {HOT_MINUTES} minutes, so I stopped "
                   "Jarvis. Check the fan, then start him again from the power page.")
            hot = 0
        # Jarvis renews the Tailscale certificate; pick the new one up (systemd restarts us)
        if cert and os.path.exists(cert[0]) and os.path.getmtime(cert[0]) != cert_mtime:
            log("certificate renewed - restarting to load it")
            os._exit(0)


def serve():
    st = load_state()
    if st.get("desired", "on") == "on" and jarvis_state() not in ("active", "activating"):
        set_jarvis(True, "restored after boot")
    fqdn = None
    for _ in range(30):                     # Tailscale may still be coming up at boot
        fqdn = tailscale_name()
        if fqdn:
            break
        time.sleep(2)
    cert = cert_files(fqdn)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*cert)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=guard, args=(cert,), daemon=True).start()
    log(f"power page on {'https' if cert else 'http'}://{fqdn or 'this-pi'}:{PORT}/ "
        f"(Jarvis is {jarvis_state()}, wanted {st.get('desired')})")
    server.serve_forever()


def nightly():
    if jarvis_state() in ("active", "activating"):
        ok, msg = set_jarvis(False, f"nightly stop ({NIGHTLY})")
        if ok:
            notify("Jarvis is off for the night", f"Stopped at {NIGHTLY} as scheduled. Tap to start him again.",
                   start_button=True)
    else:
        log("nightly: Jarvis already stopped")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "serve":
        serve()
    elif cmd in ("start", "stop"):
        ok, msg = set_jarvis(cmd == "start", "command line")
        print(msg)
        sys.exit(0 if ok else 1)
    elif cmd == "nightly":
        nightly()
    elif cmd == "link":
        fqdn = tailscale_name()
        print(f"{base_url(fqdn) if fqdn else 'http://<pi>:' + str(PORT)}/?key={load_state()['key']}")
        print("Open it once on your phone (it remembers the key), then add it to your home screen.")
    elif cmd == "status":
        print(json.dumps(status(), indent=1))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
