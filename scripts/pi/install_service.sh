#!/bin/bash
# Runs Jarvis as a systemd service on the Pi, with its power switch:
#   jarvis.service          the brain (restarts after crashes; NOT started by systemd at boot)
#   jarvis-control.service  the Start/Stop page on port 5152 + heat guard; restores Jarvis's
#                           on/off state after a reboot (scripts/pi/control.py)
#   jarvis-nightly.timer    stops Jarvis every night at 11 PM in case it was left on
#   /etc/sudoers.d/jarvis-control   lets the switch start/stop jarvis.service without a password
#
#     bash scripts/pi/install_service.sh            install (or update) and start
#     bash scripts/pi/install_service.sh remove     stop and remove all of it
#
# Afterwards:  journalctl -u jarvis -f  ·  python3 scripts/pi/control.py link  (phone link)

set -e
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
SD=/etc/systemd/system
UNITS="jarvis.service jarvis-control.service jarvis-nightly.service jarvis-nightly.timer"
SUDOERS=/etc/sudoers.d/jarvis-control

if [ "$1" = "remove" ]; then
    sudo systemctl disable --now jarvis-nightly.timer jarvis-control.service jarvis.service 2>/dev/null || true
    for u in $UNITS; do sudo rm -f "$SD/$u"; done
    sudo rm -f "$SUDOERS"
    sudo systemctl daemon-reload
    echo "Jarvis services removed."
    exit 0
fi

[ -x .venv/bin/python ] || { echo "Run scripts/pi/setup_pi.sh first."; exit 1; }
[ -f .env ] || { echo "No .env here: copy it from the PC first (scripts/pi/push_to_pi.ps1)."; exit 1; }

for u in $UNITS; do
    sed -e "s|__USER__|$USER|g" -e "s|__HOME__|$HOME|g" -e "s|__ROOT__|$ROOT|g" \
        "scripts/pi/$u" | sudo tee "$SD/$u" >/dev/null
done

# The switch runs as you and may start/stop exactly this one service with sudo, nothing else
SC=/usr/bin/systemctl; [ -x "$SC" ] || SC="$(command -v systemctl)"
TMP="$(mktemp)"
echo "$USER ALL=(root) NOPASSWD: $SC start jarvis.service, $SC stop jarvis.service, $SC restart jarvis.service" > "$TMP"
if sudo visudo -cf "$TMP" >/dev/null; then
    sudo install -m 0440 -o root -g root "$TMP" "$SUDOERS"
else
    echo "!! sudoers rule failed to validate - the Start/Stop buttons won't work"; rm -f "$TMP"; exit 1
fi
rm -f "$TMP"

sudo systemctl daemon-reload
sudo systemctl disable jarvis.service 2>/dev/null || true     # the switch decides, not boot
sudo systemctl enable --now jarvis-nightly.timer
sudo systemctl enable jarvis-control.service
sudo systemctl restart jarvis-control.service                  # starts Jarvis too (state "on")
sleep 4

echo
systemctl --no-pager --lines=5 status jarvis-control || true
echo
echo "Jarvis: $(systemctl is-active jarvis.service)   (journalctl -u jarvis -f to follow)"
echo "Nightly stop: $(systemctl list-timers jarvis-nightly.timer --no-pager | sed -n 2p | awk '{print $1, $2, $3}')"
TZ_NOW="$(timedatectl show -p Timezone --value 2>/dev/null)"
echo "Pi time zone: $TZ_NOW   (11 PM is in this zone)"
case "$TZ_NOW" in
    Etc/UTC|UTC|Europe/London|"")
        echo "!! That's probably not your time zone. Fix it with e.g.:  sudo timedatectl set-timezone Pacific/Honolulu" ;;
esac
echo
echo "Your phone's power page (open once, then add it to the home screen):"
python3 scripts/pi/control.py link
