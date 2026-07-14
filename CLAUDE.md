# CLAUDE.md

Guidance for Claude Code working in this repo. Aria is a modular personal AI assistant (Gmail triage, news brief, reminders, autonomous chores) built on LangChain/LangGraph.

## Orientation
- **[README.md](README.md)** — public showcase: what it is and why it's interesting.
- **[docs/architecture.md](docs/architecture.md)** — how it's built (memory, agent, router, engine, repo map).
- **[docs/setup.md](docs/setup.md)** — full setup & configuration reference.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — conventions + the "add a skill" recipe.
- **[context/implementation_plan.md](context/implementation_plan.md)** — design rationale, phased (dated) roadmap.
- **[context/task.md](context/task.md)** — task checklist / current status.
- **[docs/adr/](docs/adr/)** — immutable decision records (model router, 3-tier memory, Gmail security).

## Run / dev commands
```bash
source venv/bin/activate
python3 telegram_bot.py # chat from your phone (long-polling; needs TELEGRAM_* in .env)
python3 imessage_bot.py # chat via iMessage on a Mac (polls Messages; needs IMESSAGE_ALLOWED_HANDLES)
python3 interact.py     # local terminal chat
python3 voice.py        # local voice REPL (on-device Whisper STT + say/piper TTS; --ptt)
python3 voice_live.py   # realtime Gemini Live voice (barge-in; escalate_to_aria → brain)
webvoice/run.sh         # browser/phone voice client + HTTPS tunnel (ad-hoc; the always-on way
                        # is the com.aria.webvoice launchd service + Tailscale serve. Client
                        # defaults to FREE push-to-talk (local Whisper+say); Gemini Live is a toggle)
./run.sh                # nightly compaction + morning briefing email (cron entry point)
python3 clean_inbox.py  # one-shot bulk inbox cleanup
python3 aria_server.py  # FastAPI webhook server on :8000
```
Run tests with `python3 -m unittest discover tests` (offline; Gmail/LLM/network mocked). Agent behavior still needs manual verification (see CONTRIBUTING "Testing & verification").

## Architecture in one breath
Interfaces — `telegram_bot.py` (phone, long-polling), `imessage_bot.py` (iMessage on a Mac, polls `chat.db`), `interact.py` (terminal REPL), `main.py` (08:00 briefing via launchd), and the voice front-ends (`voice.py` local Whisper REPL, `voice_live.py` realtime Gemini Live, and `webvoice/` a browser/phone client) — all share one agent definition in `agent_core.py` (tools + system prompt). The realtime/voice front-ends keep the conversation snappy and hand real work to that shared brain via the `escalate_to_aria` tool. The agent calls skills in `skills/`, all of which get their LLM from `llm_router.get_llm()` (Claude→Gemini fallback) and their Gmail from `email_manager.get_gmail_service()`. Memory is a 3-tier system in `memory.py` + the two compaction scripts. Conversations persist via the SQLite checkpointer (`aria_checkpoints.db`). The bot also hosts `engine.py` — a proactivity thread of polling monitors (reminders, important email, Netflix) with quiet hours, see ADR 0004. **New interfaces and skills build on `agent_core.py`; new monitors subclass `Monitor` in `engine.py`. Don't re-define tools or the system prompt elsewhere.**

## Hard rules (don't violate without an ADR)
- **LLM access only via `llm_router.get_llm()`** — never instantiate a provider SDK in a skill (sole exception: `clean_inbox.py`, by design). See ADR 0001.
- **Gmail access only via `email_manager.get_gmail_service()`**; **sending only via `send_email()`** (allowlist-enforced, fail-safe-empty). Don't widen OAuth scopes — they're deliberately `gmail.modify` + `gmail.send` so Aria *cannot* permanently delete mail. See ADR 0003.
- **Build paths from `os.path.dirname(__file__)`** — code runs from cron with an unpredictable CWD.
- **Never commit secrets** — `.env`, `credentials.json`, `token*.json`, `profile.json` are gitignored. Keep them so.

## Runtime/generated artifacts (gitignored — don't commit)
`chroma_db/`, `cold_storage/`, `reports/`, `daily_scratchpad.txt`, `aria_calendar.db`, `aria_checkpoints.db` (LangGraph conversation checkpoints), `token*.json`.

## Known rough edges
- If the GCS allowlist can't load, `send_email` fail-safes to sending nothing (by design, ADR 0003). The morning job now fail-louds via `notify.send_telegram`, but interactive sends can still silently no-op — check logs for `SECURITY BLOCK`.
- The bot runs under launchd (`launchd/*.plist` → `~/Library/LaunchAgents`); don't also run `telegram_bot.py` manually — two pollers on one token cause Telegram 409 conflicts.
- Run only ONE engine-hosting interface at a time (`telegram_bot.py` or `imessage_bot.py`): each starts its own `engine.py` proactivity thread, so running both doubles digests/reminders and has two processes writing `aria_checkpoints.db`. Set `ARIA_ENGINE_DISABLED=1` on the secondary.
