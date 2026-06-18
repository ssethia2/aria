#!/usr/bin/env bash
# Cloudflare Tunnel → the local webvoice backend (127.0.0.1:8800), giving phones a stable
# HTTPS URL on YOUR OWN domain (e.g. ariaai.live). Cloudflare Tunnel is free and supports
# custom domains — unlike free ngrok, which only gives a *.ngrok-free.app subdomain.
#
# Recommended setup (remotely-managed tunnel — ~2 min, no local config file):
#   1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel ("aria").
#   2. Add a Public Hostname:  ariaai.live  →  service  http://localhost:8800
#      (this also creates the DNS record). Requires the domain to be on Cloudflare DNS.
#   3. Copy the tunnel token into .env:   CLOUDFLARE_TUNNEL_TOKEN=eyJ...
# This script then just runs it. With nothing set it falls back to a throwaway
# *.trycloudflare.com URL (handy for a quick local test; NOT stable, changes each run).
set -e
cd "$(dirname "$0")/../.."          # repo root
set -a; [ -f .env ] && . ./.env; set +a
PORT="${WEBVOICE_PORT:-${PORT:-8800}}"

if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
  # Remotely-managed: token encodes the tunnel + creds; ingress/hostname live in the dashboard.
  exec cloudflared --no-autoupdate tunnel run --token "$CLOUDFLARE_TUNNEL_TOKEN"
elif [ -n "${ARIA_CF_TUNNEL:-}" ]; then
  # Locally-managed named tunnel: ingress in ~/.cloudflared/config.yml (see HOSTING.md).
  exec cloudflared --no-autoupdate tunnel run "$ARIA_CF_TUNNEL"
else
  echo "No CLOUDFLARE_TUNNEL_TOKEN / ARIA_CF_TUNNEL set — using a throwaway trycloudflare URL" >&2
  echo "(fine for a quick test; set up a named tunnel for a stable ariaai.live)." >&2
  exec cloudflared --no-autoupdate tunnel --url "http://localhost:$PORT"
fi
