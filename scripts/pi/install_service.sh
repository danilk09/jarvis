#!/bin/bash
# Runs Jarvis as a systemd service on the Pi: starts at boot, restarts after crashes.
#
#     bash scripts/pi/install_service.sh            install (or update) and start it
#     bash scripts/pi/install_service.sh remove     stop and remove it
#
# Afterwards:  sudo systemctl status jarvis  ·  journalctl -u jarvis -f  ·  sudo systemctl restart jarvis

set -e
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
UNIT=/etc/systemd/system/jarvis.service

if [ "$1" = "remove" ]; then
    sudo systemctl disable --now jarvis 2>/dev/null || true
    sudo rm -f "$UNIT"
    sudo systemctl daemon-reload
    echo "Jarvis service removed."
    exit 0
fi

[ -x .venv/bin/python ] || { echo "Run scripts/pi/setup_pi.sh first."; exit 1; }
[ -f .env ] || { echo "No .env here: copy it from the PC first."; exit 1; }

sed -e "s|__USER__|$USER|g" -e "s|__HOME__|$HOME|g" -e "s|__ROOT__|$ROOT|g" \
    scripts/pi/jarvis.service | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable jarvis
sudo systemctl restart jarvis
sleep 3
systemctl --no-pager --lines=15 status jarvis || true
echo
echo "Follow the log with:  journalctl -u jarvis -f"
