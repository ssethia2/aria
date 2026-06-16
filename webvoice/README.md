# webvoice — browser voice client (spike)

A working prototype of the **phone-direct voice architecture**: the browser holds the
realtime Gemini Live session (low latency, OS does the echo cancellation + barge-in), and
the Aria brain is a backend HTTP call the model reaches via one tool, `escalate_to_aria`.

It's the same two-layer design as `voice_live.py` — Gemini up front, Claude brain behind
the escalate handoff — but the front layer runs in the browser, which is what makes it work
on a phone.

## What this spike proved
- **iOS WebKit does clean AEC + barge-in.** Tested on iPhone (Safari/Chrome — both WebKit):
  no self-echo on speaker, you can interrupt her. The thing native desktop Python couldn't do.
- **Ephemeral tokens work** — the browser opens Live with a short-lived token; the API key
  never leaves the server (`/live-token`).
- **The escalate handoff works** — Gemini calls `escalate_to_aria` → `POST /agent` → the full
  Claude agent (memory, email, calendar, tools) → spoken back. Snappy.

## Run it
```bash
source venv/bin/activate
uvicorn webvoice.server:app --host 0.0.0.0 --port 8800
```
Desktop test: open http://localhost:8800 (localhost is a secure context, so mic works).

Phone test (iOS needs HTTPS for mic): tunnel it and open the https URL on the phone:
```bash
ngrok http 8800          # or: cloudflared tunnel --url http://localhost:8800
```

Needs `GEMINI_API_KEY` (Live + token) and `ANTHROPIC_API_KEY` (the brain) in `.env`.
Optional `ARIA_LIVE_MODEL` (default is the 2.5 native-audio preview).

## Status: prototype, not production
- Single shared conversation thread (`web-voice`), **no auth** — do not expose publicly.
- The session config (system prompt + escalate tool) is baked into the ephemeral token
  server-side so the model is guaranteed the tool.
- Next steps toward a real client: auth, per-user threads, push notifications (proactivity),
  and a native/installable shell (see the phone-client plan).
