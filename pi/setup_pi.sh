#!/bin/bash
# Aria Raspberry Pi setup — run ON THE PI, from the repo root, after cloning.
# Requires: Raspberry Pi OS (64-bit) — Playwright and onnxruntime need arm64.
set -e

echo "=== Aria Pi setup ==="
echo "[1/5] System packages..."
sudo apt update
sudo apt install -y python3-venv python3-pip git

echo "[2/5] Python environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "[3/5] Playwright Chromium (for the Netflix skill)..."
python -m playwright install-deps chromium || true
if ! python -m playwright install chromium; then
    echo "⚠️  Playwright's chromium build failed on this arm64 image."
    echo "    Fallback: sudo apt install -y chromium && see docs/pi-migration.md §Playwright"
fi

echo "[4/5] systemd units..."
USER_NAME=$(whoami)
APP_DIR=$(pwd)
mkdir -p logs
for unit in pi/aria-telegram.service pi/aria-briefing.service pi/aria-briefing.timer; do
    sed -e "s|{{USER}}|$USER_NAME|g" -e "s|{{APP_DIR}}|$APP_DIR|g" "$unit" \
        | sudo tee "/etc/systemd/system/$(basename "$unit")" > /dev/null
done
sudo systemctl daemon-reload

echo "[5/5] Done. Units installed but NOT started — copy your state first!"
cat <<'EOF'

Next steps (see docs/pi-migration.md for the full checklist):
  1. STOP Aria on the Mac (two bots = Telegram 409 war):
       (on the Mac) launchctl bootout gui/$(id -u)/com.aria.telegram-bot
                    launchctl bootout gui/$(id -u)/com.aria.morning-briefing
  2. Copy secrets + state from the Mac (rsync command in the doc)
  3. Smoke test:   venv/bin/python3 -m unittest discover tests
                   venv/bin/python3 interact.py   (say hi, ask for commitments)
  4. Go live:      sudo systemctl enable --now aria-telegram.service
                   sudo systemctl enable --now aria-briefing.timer
  5. Verify:       systemctl status aria-telegram   +  message her on Telegram
EOF
