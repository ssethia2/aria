# Hosting the webvoice trial (always-on, for friends)

Goal: friends can use their invite link **anytime**, without your laptop. You need one
always-on box and a stable HTTPS URL on your domain (e.g. `ariaai.live`). HTTPS is
non-negotiable — the browser mic (`getUserMedia`) only works on a secure origin.

Two ways to expose it, depending on whether the box has a public IP:

| Host | Public IP? | How it's reached |
|------|-----------|------------------|
| **AWS EC2 / VPS** (recommended) | yes | serve directly — **Caddy** in front for auto-HTTPS |
| **Home Raspberry Pi / laptop** | no (behind NAT) | a **Cloudflare Tunnel** (no open ports) |

The backend always binds to `127.0.0.1:8800`; only the front layer (Caddy or the tunnel)
faces the internet, and `/agent` + `/live-token` are invite-gated.

---

## A. AWS EC2 / VPS + Caddy  (recommended)

A box with a public IP needs no tunnel — point DNS at it and let Caddy terminate HTTPS.

```bash
# 1. clone + python env
git clone <repo> && cd personal-assistant
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

# 2. secrets
cp .env.example .env      # set GEMINI_API_KEY + ANTHROPIC_API_KEY; ARIA_PUBLIC_URL=https://ariaai.live
#    (leave the tunnel vars unset — that's what selects public-IP mode)

# 3. DNS + firewall
#    A record:  ariaai.live -> <EC2 public IP>
#    EC2 security group: open inbound 80 + 443 (80 is for Caddy's ACME cert challenge), and 22.

# 4. backend service
webvoice/deploy/install.sh        # installs aria-webvoice (uvicorn on 127.0.0.1:8800)

# 5. Caddy in front (auto-HTTPS)
sudo apt install caddy
sudo cp webvoice/deploy/Caddyfile /etc/caddy/Caddyfile     # edit the domain if not ariaai.live
sudo systemctl reload caddy
```

That's it — Caddy fetches a Let's Encrypt cert and reverse-proxies `https://ariaai.live` →
`127.0.0.1:8800`. Logs: `logs/webvoice*.log` and `journalctl -u caddy`.

**Want origin-hiding / DDoS later?** Flip `ariaai.live` to **proxied** (orange cloud) in
Cloudflare DNS and lock the security group to Cloudflare's IP ranges. It's a DNS toggle on
top of this exact setup — no rebuild. (Note: Cloudflare's free tier cuts responses >100s.)

---

## B. Home Pi / laptop + Cloudflare Tunnel  (no public IP)

A box behind NAT can't be reached directly; a Cloudflare Tunnel makes an outbound connection
and routes traffic back — free, and it works with your own domain.

```bash
# steps 1–2 as above, plus install cloudflared (brew / apt — see pkg.cloudflare.com)
# Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel ("aria").
#   Add a Public Hostname:  ariaai.live -> service http://localhost:8800  (creates the DNS record).
#   Copy the token into .env:  CLOUDFLARE_TUNNEL_TOKEN=eyJ...   (this selects tunnel mode)
webvoice/deploy/install.sh        # now also installs aria-webvoice-tunnel (cloudflared)
```

Requires the domain to be on Cloudflare DNS. **Quick local test (no domain/token):**
`webvoice/run.sh` with nothing set spins up a throwaway `*.trycloudflare.com` URL — handy for
trying it on your phone before committing to a host.

---

## Inviting friends
```bash
python3 webvoice/add_friend.py "Alice" https://ariaai.live
# → send them the printed https://…/?t=<token> link; they Add to Home Screen.
```

## Notes
- **No proactivity engine here** — webvoice builds a guest agent and never starts the
  engine, so it's safe to run alongside your Telegram bot (no double-engine conflict).
- **Cost** is on you (Gemini Live + Claude per friend), but **capped per friend per day**:
  per-kind count caps `ARIA_GUEST_DAILY_TOKENS` (Live sessions, default 12) and
  `ARIA_GUEST_DAILY_AGENT` (brain calls, default 60), PLUS an overall hard dollar ceiling
  `ARIA_GUEST_DAILY_USD` (default $5) across all actions. Over any cap the friend hears a
  "try again tomorrow" message; counts reset daily (`webvoice/usage.json`).
