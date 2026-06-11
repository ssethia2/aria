#!/bin/bash
# Aria one-command setup (macOS or Linux). Run from the repo root: bash setup.sh
# Idempotent — safe to re-run. Never overwrites an existing .env.
set -e
cd "$(dirname "$0")"

echo "🌱 Aria setup"

echo "[1/5] Python virtual environment..."
[ -d venv ] || python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip

echo "[2/5] Dependencies (this can take a few minutes)..."
pip install --quiet -r requirements.txt

echo "[3/5] Config..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "    → created .env from template — you'll fill it in below"
else
    echo "    → .env already exists, leaving it"
fi

echo "[4/5] Browser engine (for Netflix + web automation; optional)..."
python -m playwright install chromium >/dev/null 2>&1 \
    && echo "    → chromium ready" \
    || echo "    ⚠️  chromium install skipped — browser/Netflix skills will be off"

echo "[5/5] Health check (shows exactly what's left to configure)..."
echo "-----------------------------------------------------------"
python3 healthcheck.py || true
echo "-----------------------------------------------------------"

cat <<'EOF'

Next steps:
  1. Edit .env — set the 3 REQUIRED values (Telegram token, your chat id,
     Anthropic key). The healthcheck above shows what's missing.
  2. (For Gmail/Calendar) drop your Google credentials.json here, then run:
       venv/bin/python3 auth_google.py
  3. Start her:
       venv/bin/python3 telegram_bot.py
     First run with no chat id: message your bot, it replies with your id —
     paste it into .env as TELEGRAM_ALLOWED_CHAT_ID and restart.

  Re-run `venv/bin/python3 healthcheck.py` anytime to see system status.
EOF
