# Architecture

How Aria is put together, for anyone reading the code. For *why* specific choices were made, see the [decision records](adr/).

## Shape

Several thin **interfaces** share one agent; the agent calls **skills**; a background **engine** runs autonomous monitors; everything gets its model from one **router**.

| Layer | Files | Role |
|---|---|---|
| Interfaces | `telegram_bot.py`, `imessage_bot.py`, `interact.py`, `main.py` | Telegram (text+voice notes), iMessage (Mac), terminal REPL, scheduled briefing |
| Voice | `voice.py`, `voice_live.py`, `webvoice/` | On-device Whisper REPL; realtime Gemini Live (barge-in); browser/phone client — all hand off to the agent via `escalate_to_aria` |
| Agent | `agent_core.py` | One definition of tools + system prompt, shared by all interfaces |
| Skills | `skills/` | Modular capabilities, each exposing LangChain tools |
| Engine | `engine.py` | Polling monitors that act without being asked |
| Memory | `memory.py`, `people.py`, compaction scripts | 3-tier memory + people model |
| Model access | `llm_router.py` | Tiered, fallback-aware model factory |

New interfaces and skills build on `agent_core.py`; new monitors subclass `Monitor` in `engine.py`. Tools and the system prompt are defined once, in `agent_core.py`, never duplicated.

## The model router (`llm_router.py`)

`get_llm(tier=...)` returns a LangChain chat model with a built-in fallback chain (LangChain `.with_fallbacks()`), so a rate-limit or outage degrades instead of breaking. Three tiers map model strength to task:

- **heavy** — Opus → Sonnet → Gemini — agentic multi-step work (the browser)
- **standard** — Sonnet → Opus → Gemini — the chat agent, insight, news, compaction
- **light** — Gemini (free tier) → Haiku — high-frequency screening; leads with the free model and falls to cheap paid Haiku when it 429s

The chat agent's large, stable system+tools prefix is **prompt-cached** (the live timestamp is isolated in an uncached block so it can't bust the cache), cutting input cost dramatically on multi-turn chats. When a chain link starts failing, the user is alerted on Telegram once per model per day (except the light tier, whose free-primary is *expected* to fall through daily). See [ADR 0001](adr/0001-claude-gemini-fallback.md).

## Memory (`memory.py` + `nightly_compaction.py` + `dynamic_consolidation.py`)

A three-tier hierarchy — see [ADR 0002](adr/0002-3tier-memory.md):

1. **Tier 1 — working memory.** `add_memory` appends raw facts to `daily_scratchpad.txt` instantly (no embedding on the hot path).
2. **Tier 2 — semantic memory.** `nightly_compaction.py` LLM-distills the scratchpad into durable facts, embeds them into ChromaDB (`chroma_db/`), wipes the scratchpad. The distillation **filters**: facts about the user are kept; operational log lines (engine actions, fired reminders) are discarded.
3. **Tier 3 — cold storage.** When ChromaDB crosses a size threshold, `dynamic_consolidation.py` summarizes granular vectors into a long-form narrative in `cold_storage/`, deletes the originals, and leaves one "pointer" vector. `read_cold_storage` loads it on demand.

`search_memory` reads Tier 1 + Tier 2 together. `profile.json` is static identity.

### People model (`people.py`)
People are first-class records (name, relation, aliases, birthday, notes), not loose facts. `remember_person` / `get_person` / `list_people` are agent tools; saving a birthday auto-creates a yearly `people_date` commitment. Stored in `people.json`.

### Durable conversations
Each interface runs the agent with a LangGraph **SQLite checkpointer** (`aria_checkpoints.db`), so conversations survive restarts — one persistent thread per Telegram chat and for the REPL. Only the new message is sent each turn; the checkpointer supplies history.

### Standing instructions (`instructions.py`)
User-given persistent rules ("from now on…") are stored in `instructions.json` and injected into the system prompt every turn — always in force, unlike recalled memory. Aria adds/updates/removes them via tools; a curation nudge keeps the registry from becoming a junk drawer.

## The proactivity engine (`engine.py`)

A daemon thread inside the bot runs pluggable `Monitor`s, each on its own interval, with quiet hours (23:00–08:00 queues non-urgent pings into a morning digest) and per-monitor state in `engine_state.json`. Every monitor call is exception-guarded so one failure never takes down the rest. See [ADR 0004](adr/0004-polling-engine-over-webhooks.md).

| Monitor | What it does |
|---|---|
| `CommitmentMonitor` | Pings timed/recurring commitments at their moment; advances recurrences |
| `EmailDigestMonitor` | Deterministic bulk pre-filter → LLM-screens the rest → one evening digest; replies-owed become commitments |
| `ChaseMonitor` | Daytime LLM judgment over open commitments → at most one warm nudge/day |
| `InsightMonitor` | Twice-daily cross-source synthesis (calendar + commitments + weather) → one non-obvious insight, or silence |
| `HealthMonitor` | Re-runs the self-diagnosis; alerts when something turns FAIL |
| `HeartbeatMonitor` | Pings an external monitor (dead-man's-switch) so a host crash is detectable |
| `NetflixMonitor` | Handles a fresh Netflix Household email autonomously |

## Reliability (`healthcheck.py` + `heartbeat.py`)

Silent failure is the enemy. `healthcheck.py` validates secrets, email auth, memory, databases, engine freshness, whether the briefing ran, and disk — as a CLI ("doctor", non-zero exit on FAIL), a chat tool (`get_system_status`), a boot self-check, and the `HealthMonitor`. The external `HEARTBEAT_URL` ping covers the one thing internal checks can't: the whole host being dead.

## Security model

- LLM access only via `llm_router.get_llm()` — no provider SDKs in skills (ADR 0001).
- Gmail via `email_manager.get_gmail_service()`; sending only via `send_email()`, allowlist-enforced, fail-safe-empty. Scopes are deliberately `gmail.modify` + `gmail.send` — Aria cannot permanently delete mail. See [ADR 0003](adr/0003-gmail-scope-restriction.md), [ADR 0005](adr/0005-local-allowlist-fallback.md).
- The agentic browser never pays, places orders, or enters passwords — it stops at that boundary and hands off a link.
- Secrets and personal state are gitignored: `.env`, `credentials.json`, `token*.json`, `profile.json`, `allow.json`, the databases, `chroma_db/`, `cold_storage/`, `notes/`, and the JSON stores.

## Repository map

```
agent_core.py            Shared agent: tools + cached system prompt
telegram_bot.py          Telegram interface (long-poll; text + voice; progress updates)
imessage_bot.py          iMessage interface on macOS (polls chat.db; imessage_reader/_send)
interact.py              Terminal REPL
voice.py                 Local voice REPL (on-device Whisper STT + say/piper TTS)
voice_live.py            Realtime Gemini Live voice (barge-in; escalate_to_aria → brain)
voice_aec.py             Optional speexdsp echo cancellation for voice_live
webvoice/                Browser/phone voice client (Live in-browser) + FastAPI backend
main.py / morning_run.py Scheduled briefing (fail-loud delivery)
llm_router.py            Tiered, fallback-aware model factory
memory.py                Profile + ChromaDB semantic memory + memory tools
people.py                People model
instructions.py          Standing-instructions registry
engine.py                Proactivity engine + all monitors
healthcheck.py           Self-diagnosis (doctor / tool / startup / monitor)
heartbeat.py             External dead-man's-switch ping
notify.py                Outbound Telegram notifications
email_backend.py         IMAP/SMTP backend (no-Google-Cloud email path)
email_filter.py          Deterministic bulk-mail pre-filter
report_generator.py      Daily briefing Markdown
nightly_compaction.py    Tier 1 → Tier 2 memory compaction
dynamic_consolidation.py Tier 2 → Tier 3 cold-storage consolidation
setup.sh / setup_wizard.py   Self-host setup + guided .env
auth_google.py / auth_netflix.py   OAuth bootstrap
skills/
  email_manager.py       Gmail/IMAP fetch, triage, send, drafts
  news_manager.py        Newsletter aggregation
  commitment_manager.py  Commitment store + capture tools
  google_calendar.py     Calendar create/edit/delete (dual-write)
  notes_manager.py       Notes store
  grocery_manager.py     Grocery list + recipe→ingredients
  research_manager.py    Web search + page reading
  browser_manager.py     Agentic browser (explore + report, payment-safe)
  weather_manager.py     Forecast (Open-Meteo, keyless)
  package_manager.py     Package tracking from the inbox
  home_assistant.py      Smart-home (Matter via Home Assistant)
  netflix_manager.py     Netflix Household automation
launchd/                 macOS resident-service agents
pi/                      Raspberry Pi systemd units + setup
docs/                    Architecture, setup, ADRs, Pi migration
tests/                   Offline unit suite
context/                 Implementation plan + task log
```
