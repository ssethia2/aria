#!/usr/bin/env bash
# Stable HTTPS tunnel to the local webvoice backend (127.0.0.1:8800), so phones can reach it.
# One-time: reserve a free static domain at dashboard.ngrok.com → Domains, set
# ARIA_NGROK_DOMAIN in .env, and add your authtoken once: ngrok config add-authtoken <token>.
set -e
cd "$(dirname "$0")/../.."          # repo root
set -a; [ -f .env ] && . ./.env; set +a
PORT="${WEBVOICE_PORT:-8800}"
if [ -z "${ARIA_NGROK_DOMAIN:-}" ]; then
  echo "ARIA_NGROK_DOMAIN not set in .env — reserve a static domain for a STABLE url" >&2
  exec ngrok http "$PORT" --log=stdout
fi
exec ngrok http "$PORT" --url="https://${ARIA_NGROK_DOMAIN}" --log=stdout
