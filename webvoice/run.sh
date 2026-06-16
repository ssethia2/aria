#!/usr/bin/env bash
# Launch the web voice client + an HTTPS tunnel so it works on your phone.
# For a STABLE url (so "Add to Home Screen" keeps working): reserve a free static domain
# at dashboard.ngrok.com → Domains, then `export ARIA_NGROK_DOMAIN=your-name.ngrok-free.app`.
set -e
cd "$(dirname "$0")/.."
source venv/bin/activate
PORT="${PORT:-8800}"
uvicorn webvoice.server:app --host 0.0.0.0 --port "$PORT" &
UVI=$!; trap "kill $UVI 2>/dev/null" EXIT
sleep 2
if [ -n "$ARIA_NGROK_DOMAIN" ]; then
  echo "📱 Open on your phone:  https://$ARIA_NGROK_DOMAIN   (then Share → Add to Home Screen)"
  ngrok http "$PORT" --url="https://$ARIA_NGROK_DOMAIN"
else
  echo "ℹ️  Set ARIA_NGROK_DOMAIN for a stable URL. Using a random ngrok URL this run:"
  ngrok http "$PORT"
fi
