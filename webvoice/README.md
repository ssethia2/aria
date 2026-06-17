# webvoice — browser voice client (spike)

A working prototype of the **phone-direct voice architecture**: the browser holds the
realtime Gemini Live session (low latency, OS does the echo cancellation + barge-in), and
the Aria brain is a backend HTTP call the model reaches via one tool, `escalate_to_aria`.

It's the same two-layer design as `voice_live.py` — Gemini up front, Claude brain behind
the escalate handoff — but the front layer runs in the browser, which is what makes it work
on a phone.

## Letting friends try it (guest mode)

This backend is **multi-user and invite-only**. Each friend gets a link with a token; that
token maps to an isolated guest who Aria greets **by name**, with their **own** memory, their
**own** reminders/commitments, and an **account-free** toolset (memory + reminders + web +
weather — no email/calendar/contacts/etc.), in their **own** conversation thread. Friends can
never see your data or each other's. (You use telegram/`voice_live` for your full Aria.)

```bash
python3 webvoice/add_friend.py "Alice" https://your-domain.ngrok-free.app
# → Invite link: https://your-domain.ngrok-free.app/?t=<token>
```
Send that link. They open it, tap Start, and talk — Add to Home Screen for an app icon.
Re-running for the same name reuses their token, so their memory persists. Tokens live in
`webvoice/friends.json` (gitignored). Opening the site without a valid `?t=` is rejected.

## What this spike proved
- **iOS WebKit does clean AEC + barge-in.** Tested on iPhone (Safari/Chrome — both WebKit):
  no self-echo on speaker, you can interrupt her. The thing native desktop Python couldn't do.
- **Ephemeral tokens work** — the browser opens Live with a short-lived token; the API key
  never leaves the server (`/live-token`).
- **The escalate handoff works** — Gemini calls `escalate_to_aria` → `POST /agent` → the full
  Claude agent (memory, email, calendar, tools) → spoken back. Snappy.

## Use it on your phone (the easy way)

One command starts the server **and** the HTTPS tunnel the phone needs:
```bash
export ARIA_NGROK_DOMAIN=your-name.ngrok-free.app   # see "stable URL" below
webvoice/run.sh
```
It prints a URL — open it on your iPhone, then **Share → Add to Home Screen**. That drops a
tap-to-launch **Aria** icon that opens fullscreen, like a native app. Tap it, hit Start, talk.

**Stable URL (do this once):** reserve a free static domain at dashboard.ngrok.com → Domains,
and set `ARIA_NGROK_DOMAIN` to it. Without it `run.sh` uses a random ngrok URL each run, so the
home-screen icon would break — the reserved domain keeps it permanent.

Desktop check: `webvoice/run.sh` without a domain, or just
`uvicorn webvoice.server:app --port 8800` and open http://localhost:8800 (localhost allows mic
without HTTPS).

Needs `GEMINI_API_KEY` (Live + token) and `ANTHROPIC_API_KEY` (the brain) in `.env`.
Optional `ARIA_LIVE_MODEL` (default is the 2.5 native-audio preview).

> The backend has to be running for the app to work. For now that's your Mac (run `run.sh`
> when you want it); for always-on, host it on the Pi/cloud with a persistent tunnel.

## Status: prototype, not production
- Single shared conversation thread (`web-voice`), **no auth** — do not expose publicly.
- The session config (system prompt + escalate tool) is baked into the ephemeral token
  server-side so the model is guaranteed the tool.
- Next steps toward a real client: auth, per-user threads, push notifications (proactivity),
  and a native/installable shell (see the phone-client plan).
