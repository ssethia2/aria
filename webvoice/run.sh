#!/usr/bin/env bash
# Launch the web voice client + a Cloudflare Tunnel so it works on your phone.
# For a STABLE url on your own domain (so "Add to Home Screen" keeps working): set up a
# Cloudflare tunnel and put its token in .env as CLOUDFLARE_TUNNEL_TOKEN — see the ~2-min
# setup in webvoice/deploy/tunnel.sh. With nothing set you get a throwaway trycloudflare URL.
set -e
cd "$(dirname "$0")/.."
source venv/bin/activate
set -a; [ -f .env ] && . ./.env; set +a
PORT="${PORT:-8800}"
uvicorn webvoice.server:app --host 0.0.0.0 --port "$PORT" &
UVI=$!; trap "kill $UVI 2>/dev/null" EXIT
sleep 2
if [ -n "${ARIA_PUBLIC_URL:-}" ]; then
  echo "📱 Open on your phone:  $ARIA_PUBLIC_URL   (then Share → Add to Home Screen)"
fi
# Foreground (not exec) so the EXIT trap still tears down uvicorn when the tunnel stops.
PORT="$PORT" webvoice/deploy/tunnel.sh
