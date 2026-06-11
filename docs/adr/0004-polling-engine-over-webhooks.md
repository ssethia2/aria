# ADR 0004 — Polling proactivity engine over push webhooks

**Status:** Accepted (2026-06). Supersedes the open items of the Cloud API & Instant Webhook phase (Ngrok tunneling, Pub/Sub push wiring).

## Context
Phase 4 planned instant Gmail push notifications: GCP Pub/Sub → Ngrok-exposed FastAPI webhook → Netflix automation. That design requires a public endpoint, a 24/7 server, Pub/Sub topic/permission management, and watch-renewal upkeep — for a single user whose host is now a laptop. The GCP deployment it assumed is being decommissioned after an unexplained ~$54/month bill; the new hosting posture is local-first with zero public-facing infrastructure. Meanwhile, the Telegram bot already runs as a resident launchd service with an outbound notification channel (`notify.py`).

We also wanted proactivity to be *general* (reminders, important email, future monitors), not a one-off Netflix pipe.

## Decision
Replace push with **in-process polling**: a `ProactiveEngine` (engine.py) runs as a daemon thread inside the Telegram bot and drives pluggable **Monitors**, each on its own poll interval:

- `ReminderMonitor` (30 min) — pings when calendar reminders come due
- `ImportantEmailMonitor` (15 min) — lists new inbox ids cheaply; consults the LLM only when something new arrived, and interrupts only for can't-wait mail
- `NetflixMonitor` (10 min) — on a fresh household email, **acts immediately** (browser automation), then reports

Engine policies live in the engine, not the monitors: **quiet hours** (23:00–08:00) queue non-urgent notifications and flush a single "while you were away" digest in the morning; per-monitor state and the queue persist in `engine_state.json`; every monitor call is exception-guarded so one failure never affects the rest or the bot.

`aria_server.py` (FastAPI webhook receiver) is kept in the tree for a future always-on host, but is no longer the plan of record.

## Consequences
- **Latency:** worst case = poll interval (10 min for Netflix vs ~0 for push). Acceptable: household updates tolerate minutes, and "act now, report later" means even quiet hours don't delay the *action*.
- **Zero attack surface & zero infra:** no public URL, no tunnel, no Pub/Sub, nothing to renew or pay for. The failure domain collapses into one supervised process.
- **Quota cost:** a few Gmail `list` calls per hour — far below quota; the LLM is invoked only on new mail.
- **Generalized proactivity:** new monitors are one small class (`name`, `interval`, `check(state) -> [Notification]`) registered in `default_engine()` — same extension philosophy as skills.
- **Laptop-bound:** monitors only run while the laptop is awake — same availability trade we already accepted for chat. Moving to a Pi/VPS later requires no design change, just relocation.
