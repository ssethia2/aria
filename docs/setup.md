# Setup & configuration

Aria runs on your own machine with your own keys. The fastest path is `bash setup.sh` (creates the venv, installs deps, runs a guided `.env` wizard, and prints a health check). This page is the full reference.

## Requirements

- Python 3.10+
- An **Anthropic API key** ([console.anthropic.com](https://console.anthropic.com))
- A **Telegram bot** (free, via [@BotFather](https://t.me/BotFather))
- Optional, per feature: a Gemini key, Google OAuth credentials *or* an email app password, a Home Assistant token, etc.

## The three required values

Aria starts with just these; everything else degrades off cleanly until configured.

| Value | Where it comes from |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `TELEGRAM_ALLOWED_CHAT_ID` | Auto-discovered: run the bot, message it once in "setup mode", it replies with your id |

Run `venv/bin/python3 healthcheck.py` anytime to see exactly what's configured and what's missing.

## Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. `venv/bin/python3 telegram_bot.py`, then message your bot. With no allowlist set it runs in **setup mode** and replies with your chat id.
3. Put that id in `.env` as `TELEGRAM_ALLOWED_CHAT_ID` (comma-separate for multiple) and restart. Only allowlisted chats are served — **required**, since anyone who finds the bot could otherwise message it, and Aria can read your email.

Long-polling means **no public endpoint, port, or tunnel** — it runs anywhere, reachable from your phone.

## Email — pick one path

**Easy (no Google Cloud project):** enable 2-step verification on your email, create an **app password**, and set `EMAIL_APP_PASSWORD` (+ `USER_EMAIL`) in `.env`. Works with Gmail, Fastmail, iCloud — any IMAP/SMTP host (`IMAP_HOST`/`SMTP_HOST` default to Gmail). Covers inbox triage, briefing send, and reply drafts.

**Richer (Gmail API):** drop a Google OAuth **Desktop** `credentials.json` in the project root and run `python3 auth_google.py`. Adds newsletter aggregation, carrier package search, label-based triage, and the Netflix skill. Leave `EMAIL_APP_PASSWORD` blank to use this path.

> **Security:** Aria can only *send* to addresses on an allowlist (`allow.json`, or `USER_EMAIL` as a fallback), and the OAuth scopes are deliberately `gmail.modify` + `gmail.send` — it can label and archive but **cannot permanently delete** mail. See [ADR 0003](adr/0003-gmail-scope-restriction.md).

## Calendar

With the Gmail-API path set up (`auth_google.py` requests the calendar scopes too), tell Aria *"set up my calendars"* in chat — it lists your calendars and you pick which is shared. Calendar create/edit/delete then work. (The Google Cloud project must have the **Calendar API** enabled.)

## Optional features

| Feature | Config |
|---|---|
| Semantic memory + LLM fallback | `GEMINI_API_KEY` ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) |
| Smart home (Matter via Home Assistant) | `HA_URL`, `HA_TOKEN` |
| External dead-man's-switch | `HEARTBEAT_URL` (a [healthchecks.io](https://healthchecks.io) ping URL; period ~20m, grace ~10m) |
| iMessage interface (macOS) | `IMESSAGE_ALLOWED_HANDLES` + Full Disk Access & Automation→Messages — see [imessage.md](imessage.md). Run only one engine-hosting interface at a time. |
| Local voice REPL (`voice.py`) | `pip install sounddevice`; `ARIA_WHISPER_MODEL` = `base` (default) or `tiny` (slower CPUs / Pi) for STT size |
| Realtime voice (`voice_live.py`, `webvoice/`) | `GEMINI_API_KEY` (powers Gemini Live); optional `ARIA_LIVE_MODEL` to override the model. `webvoice/run.sh` + ngrok serves it to your phone (PWA). |
| Voice barge-in on speakers (optional) | `brew install speexdsp` for echo cancellation in `voice_live.py --aec`; otherwise headphones (`--duplex`) or the default mic-gate |
| Disable the proactivity engine | `ARIA_ENGINE_DISABLED=1` |
| Netflix Household automation | second Gmail account: `python3 auth_netflix.py` → `token_netflix.json` |

See [`.env.example`](../.env.example) for the annotated list.

## Running it as a service

**macOS (launchd):** `launchd/` has two agents — the bot (starts at login, auto-restarts on crash) and the 08:00 briefing (fires on wake if the machine was asleep):
```bash
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aria.telegram-bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aria.morning-briefing.plist
```
Logs land in `logs/`. Don't also run `telegram_bot.py` manually — two pollers on one token cause Telegram 409 conflicts.

**Raspberry Pi / Linux (systemd):** see [pi-migration.md](pi-migration.md) — `pi/` has the units and `setup_pi.sh`, plus the state-transfer checklist for moving an existing install onto an always-on host.

## Reliability

Run `python3 healthcheck.py` to validate the whole system (secrets, email auth, memory, databases, engine freshness, whether the briefing ran, disk; non-zero exit on failure). The bot self-checks on boot and Telegrams you if it restarted broken; the engine's `HealthMonitor` alerts when something turns FAIL; and `HEARTBEAT_URL` covers a full host-down. See [architecture.md](architecture.md#reliability-healthcheckpy--heartbeatpy).
