#!/usr/bin/env bash
# Install the webvoice systemd services on an always-on Linux host (Pi or VPS).
# Run from the repo root after cloning + setting up venv + .env. Needs sudo.
set -e
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
mkdir -p "$APP_DIR/logs"
for unit in aria-webvoice aria-webvoice-tunnel; do
  sed "s|{{USER}}|$USER_NAME|g; s|{{APP_DIR}}|$APP_DIR|g" \
    "$APP_DIR/webvoice/deploy/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now aria-webvoice aria-webvoice-tunnel
echo "✅ webvoice + tunnel running. Logs: $APP_DIR/logs/webvoice*.log"
echo "Add a friend:  python3 webvoice/add_friend.py \"Name\" https://\$ARIA_NGROK_DOMAIN"
