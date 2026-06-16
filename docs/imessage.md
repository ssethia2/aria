# Aria on iMessage

`imessage_bot.py` is an alternative to `telegram_bot.py` for running Aria on a Mac and
talking to her over iMessage. It reuses the same agent, memory, skills, and engine; only
the transport differs. There is no iMessage API, so it works by reading the local Messages
database (`chat.db`) and sending through Messages.app via AppleScript. That means **Aria
must run on a Mac that stays on and signed into Messages.**

```
your phone ──iMessage──▶  Mac running Messages.app
                              │  imessage_reader.py  ← polls chat.db (inbound)
                              ▼
                          agent_core.py  (same agent as Telegram)
                              │
                          imessage_send.py → AppleScript → Messages.app ──▶ your phone
```

## 1. Give Aria its own iMessage identity (recommended)

The cleanest setup is a **dedicated Apple ID** the Mac signs into for Messages, so messages
from *your* phone arrive as inbound (`is_from_me = 0`). If the Mac is signed into your *own*
Apple ID, texts you send from your phone sync as your own sent messages and the bot can't
cleanly tell an incoming request from your own outgoing text.

1. Create a new Apple ID (e.g. `aria.yourname@icloud.com`).
2. On the Mac: **Messages → Settings → iMessage → sign in** with it.
3. From your phone, text that account once to confirm a blue bubble arrives.

## 2. Grant the two macOS permissions

The process that runs the bot (Terminal/iTerm, or the Python the launchd job invokes) needs:

- **Full Disk Access** — to read `~/Library/Messages/chat.db`.
  System Settings → Privacy & Security → **Full Disk Access**.
- **Automation → Messages** — to send via AppleScript (macOS prompts on first send).

Quick check:
```bash
python3 imessage_reader.py                          # prints inbound messages as they arrive
python3 imessage_send.py "+15551234567" "test"      # should land on your phone
```

## 3. Configure and run

```bash
venv/bin/python3 imessage_bot.py
```

Leave `IMESSAGE_ALLOWED_HANDLES` blank on first run: the bot is in **setup mode** and texts
back the handle of whoever messages it. Paste that into `.env`:

```
IMESSAGE_ALLOWED_HANDLES=+15551234567      # comma-separate for multiple
```

Restart. Now only your handle is served; everyone else is ignored. The allowlist is
**required** — Aria can read your email and calendar, so the account must not be drivable by
strangers. `IMESSAGE_PROGRESS_NOTES` controls the "what I'm doing" bubbles: `first`
(default), `all` (one per tool family, like Telegram), or `off`.

## Notes & limits

- **Text only for now.** iMessage audio messages (the Whisper transcription + spoken
  replies the Telegram interface has) aren't wired yet — a natural follow-up: read the
  `attachment` table and reuse `llm_router.transcribe_audio`.
- **launchd:** a process under launchd has different TCC grants than your Terminal. If
  reads/sends work interactively but fail under launchd, grant Full Disk Access +
  Automation to the venv's `python3`, not just Terminal. Don't run two pollers at once.
