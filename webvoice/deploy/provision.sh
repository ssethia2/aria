#!/usr/bin/env bash
# One-shot provisioning for the Aria webvoice guest backend on a FRESH Ubuntu 24.04 EC2 box
# (public-IP host + Caddy auto-HTTPS). Run as the default 'ubuntu' user; it sudo's where needed.
# Safe to re-run (idempotent-ish).
#
# PREREQUISITES (do these first):
#   1. DNS: A record  ariaai.live -> this box's Elastic IP  (confirm: dig +short ariaai.live).
#   2. Security group: inbound 22 (your IP), 80 and 443 (0.0.0.0/0).
#   3. Get your .env onto the box — it already has the API keys, caps, and ARIA_PUBLIC_URL.
#      From your Mac, in the repo dir:
#        scp -i <your-key>.pem .env ubuntu@ariaai.live:~/aria.env
#   4. Repo access: export REPO_URL to something this box can clone, e.g. an HTTPS URL with a
#      GitHub token:   export REPO_URL='https://<TOKEN>@github.com/ssethia2/aria.git'
#      (or set up an SSH deploy key and use the git@ URL — the default below).
#
# Then:  curl/paste this script and run  bash provision.sh
set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:ssethia2/aria.git}"
APP_DIR="${APP_DIR:-$HOME/personal-assistant}"

echo "==> System packages"
sudo apt-get update -y
sudo apt-get install -y git python3-venv python3-pip python3-dev build-essential curl \
  debian-keyring debian-archive-keyring apt-transport-https

echo "==> 2 GB swap (insurance on a 2 GB box)"
if ! sudo swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> Clone / update repo"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p logs

echo "==> .env"
if [ ! -f .env ]; then
  if [ -f "$HOME/aria.env" ]; then
    mv "$HOME/aria.env" .env && echo "   moved ~/aria.env -> .env"
  else
    echo "!! No .env here and ~/aria.env not found. scp it up (see PREREQUISITES) and re-run." >&2
    exit 1
  fi
fi

echo "==> Python venv + deps (a few minutes)"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Backend service (uvicorn on 127.0.0.1:8800)"
webvoice/deploy/install.sh      # public-IP mode: installs aria-webvoice (+ a Caddy reminder)

echo "==> Caddy (auto-HTTPS reverse proxy)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y caddy
fi
sudo cp webvoice/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy

cat <<'DONE'

✅ Done. Verify:
   systemctl status aria-webvoice --no-pager
   journalctl -u caddy -n 20 --no-pager        # watch the cert get issued
   curl -I https://ariaai.live                 # expect HTTP/2 200

Invite a friend:
   cd ~/personal-assistant && source venv/bin/activate
   python3 webvoice/add_friend.py "Alice" https://ariaai.live
DONE
