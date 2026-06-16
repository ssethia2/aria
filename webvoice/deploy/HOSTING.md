# Hosting the webvoice trial (always-on, for friends)

Goal: friends can use their invite link **anytime**, without your laptop. You need one
always-on Linux box and a stable HTTPS URL. A **~$5/mo VPS** (DigitalOcean, Hetzner, etc.)
or the **Raspberry Pi** both work. Two systemd services run it: the backend (`uvicorn`,
bound to localhost) and a tunnel that gives it a stable public HTTPS URL.

## One-time setup on the host

```bash
# 1. clone + python env
git clone <repo> && cd personal-assistant
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

# 2. secrets + the stable domain
cp .env.example .env      # set GEMINI_API_KEY and ANTHROPIC_API_KEY
#   reserve a free static domain at https://dashboard.ngrok.com → Domains, then add to .env:
#   ARIA_NGROK_DOMAIN=your-name.ngrok-free.app

# 3. install ngrok + its authtoken (free account)
#   (download ngrok for your platform), then:
ngrok config add-authtoken <your-token>

# 4. install + start the services
webvoice/deploy/install.sh
```

That brings up:
- **`aria-webvoice`** — `uvicorn` on `127.0.0.1:8800` (not publicly exposed itself).
- **`aria-webvoice-tunnel`** — ngrok serving `https://$ARIA_NGROK_DOMAIN` → the backend.

Logs: `logs/webvoice*.log`. Manage with `systemctl status/restart aria-webvoice`.

## Inviting friends
```bash
python3 webvoice/add_friend.py "Alice" https://$ARIA_NGROK_DOMAIN
# → send them the printed https://…/?t=<token> link; they Add to Home Screen.
```

## Notes
- **Localhost binding + tunnel** keeps the backend off the open internet; only the tunnel
  (HTTPS, the URL friends know) reaches it, and `/agent` + `/live-token` are invite-gated.
- **No proactivity engine here** — webvoice builds a guest agent and never starts the
  engine, so it's safe to run alongside your Telegram bot (no double-engine conflict).
- **Cost** is on you (Gemini Live + Claude per friend). Per-friend caps land in Phase 4;
  until then, watch usage with a handful of friends.
- **Alternative to ngrok:** a VPS with your own domain + Caddy (auto-HTTPS reverse proxy
  to `127.0.0.1:8800`) instead of the tunnel service — swap step 3/4 accordingly.
