#!/usr/bin/env bash
# Install the webvoice systemd service(s) on an always-on Linux host (VPS/EC2 or a Pi).
# Run from the repo root after cloning + setting up venv + .env. Needs sudo.
#
# Always installs the backend (uvicorn on 127.0.0.1:8800). For the PUBLIC URL it picks a mode
# from .env:
#   - public-IP host (EC2/VPS): leave the tunnel vars unset and put Caddy in front (this
#     script reminds you how — see webvoice/deploy/Caddyfile).
#   - no public IP (home Pi/laptop): set CLOUDFLARE_TUNNEL_TOKEN (or ARIA_CF_TUNNEL) and this
#     also installs the cloudflared tunnel service.
set -e
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
mkdir -p "$APP_DIR/logs"
set -a; [ -f "$APP_DIR/.env" ] && . "$APP_DIR/.env"; set +a

install_unit() {
  sed "s|{{USER}}|$USER_NAME|g; s|{{APP_DIR}}|$APP_DIR|g" \
    "$APP_DIR/webvoice/deploy/$1.service" | sudo tee "/etc/systemd/system/$1.service" >/dev/null
}

UNITS="aria-webvoice"
if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}${ARIA_CF_TUNNEL:-}" ]; then
  UNITS="$UNITS aria-webvoice-tunnel"     # no-public-IP mode: cloudflared tunnel
fi
for unit in $UNITS; do install_unit "$unit"; done

sudo systemctl daemon-reload
sudo systemctl enable --now $UNITS
echo "✅ running: $UNITS   (logs: $APP_DIR/logs/webvoice*.log)"

if ! echo "$UNITS" | grep -q tunnel; then
  cat <<EOF
ℹ️  Public-IP mode: backend is on 127.0.0.1:8800 — put Caddy in front for HTTPS:
      sudo apt install caddy
      sudo cp $APP_DIR/webvoice/deploy/Caddyfile /etc/caddy/Caddyfile   # edit the domain
      sudo systemctl reload caddy
    Then point your domain's A record at this server and open ports 80+443.
EOF
fi
echo "Add a friend:  python3 webvoice/add_friend.py \"Name\" \$ARIA_PUBLIC_URL"
