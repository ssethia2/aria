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
| Memory | `memory.py`, `people.py`, `nightly_compaction.py` | scratchpad + dated semantic memory + people model |
| Model access | `llm_router.py` | Tiered, fallback-aware model factory |

New interfaces and skills build on `agent_core.py`; new monitors subclass `Monitor` in `engine.py`. Tools and the system prompt are defined once, in `agent_core.py`, never duplicated.

## The model router (`llm_router.py`)

`get_llm(tier=...)` returns a LangChain chat model with a built-in fallback chain (LangChain `.with_fallbacks()`), so a rate-limit or outage degrades instead of breaking. Three tiers map model strength to task:

- **heavy** — Opus 5.5 → Sonnet 5.5 → Gemini — the owner's API chat agent (the subscription brain's stand-in, so the same model) and the browser
- **standard** — Sonnet 5.5 → Opus 5.5 → Gemini — guests' chat agent, news, compaction, photos
- **light** — Haiku 5.5 → Gemini (free tier) — the background judges (email screening, heads-ups, nudges, reflections) and the chat fast path

The 5.5 models think before answering, so a reply can start with thinking blocks: read replies with `reply_text()`, never `.content` as a string. The API agent rebuilds its system prompt on every call, which changes the prefix a replayed thinking block is bound to, so the router asks the API to drop such a block rather than reject the request (`prefix_mismatch_behavior: drop_block`).

The chat agent's large, stable system+tools prefix is **prompt-cached** (the live timestamp is isolated in an uncached block so it can't bust the cache), cutting input cost dramatically on multi-turn chats. When a chain link starts failing, the user is alerted on Telegram once per model per day (except the light tier, whose fall-through to free Gemini is low-stakes). See [ADR 0001](adr/0001-claude-gemini-fallback.md).

## Memory (`memory.py` + `nightly_compaction.py`)

Two tiers over a static profile — see [ADR 0002](adr/0002-3tier-memory.md) and [ADR 0008](adr/0008-retire-cold-storage-tier.md), which retired the original cold-storage tier:

1. **Tier 1 — working memory.** `add_memory` appends raw facts to `daily_scratchpad.txt` instantly (no embedding on the hot path), and every exchange with the owner is appended to `daily_conversation.jsonl` ([ADR 0010](adr/0010-conversations-are-memory.md)).
2. **Tier 2 — semantic memory.** `nightly_compaction.py` LLM-distills the scratchpad and the day's conversations into durable facts and stores them in ChromaDB (`chroma_db/`) via `memory.store_facts()`, each dated by when it was noted, then wipes the scratchpad. The distillation **filters**: facts about the user are kept; operational log lines (engine actions, fired reminders) are discarded. The store is uncapped — nothing is summarized away.

Each substantive owner message reaches the brain with `memory.recall()` prepended (dated matches, no tool call needed). `search_memory` reads Tier 1 (the scratchpad, plus logged exchanges that match the query) plus the top 5 Tier-2 matches, each shown with its date so the newer of two conflicting facts wins. `profile.json` is static identity.

### People model (`people.py`)
People are first-class records (name, relation, aliases, birthday, notes), not loose facts. `remember_person` / `get_person` / `list_people` are agent tools; saving a birthday auto-creates a yearly `people_date` commitment. Stored in `people.json`.

### Durable conversations
Each interface runs the agent with a LangGraph **SQLite checkpointer** (`aria_checkpoints.db`), so conversations survive restarts — one persistent thread per Telegram chat and for the REPL. Only the new message is sent each turn; the checkpointer supplies history.

### Standing instructions (`instructions.py`)
User-given persistent rules ("from now on…") are stored in `instructions.json` and injected into the system prompt every turn — always in force, unlike recalled memory. Aria adds/updates/removes them via tools; a curation nudge keeps the registry from becoming a junk drawer.

## The proactivity engine (`engine.py`)

A daemon thread inside the bot runs pluggable `Monitor`s, each on its own interval, with quiet hours (23:00–08:00 queues non-urgent pings into a morning digest) and per-monitor state in `engine_state.json`. Every monitor call is exception-guarded so one failure never takes down the rest. See [ADR 0004](adr/0004-polling-engine-over-webhooks.md).

**One task, many parts (`situation.py`).** A commitment row is only one moving part of a task; the reply he sent, the invite that came back and the event on his calendar are the others. `situation.around()` gathers the recent mail (sent and received) and the next 30 days of calendar around each open commitment, in a few batched Gmail calls. Everything that speaks about commitments reads it: `engine.reconcile()` closes items the evidence shows are handled (before every chase check and before the briefing lists what's due), the chase judge sees it before nudging, `list_commitments` returns it to the chat brain, and the email screener sees each email's thread so far plus the calendar and open commitments. The model proposes a close; code checks it: the evidence must exist and name the item, a reply must be his and newer than the item, and a calendar event only closes an item about getting it booked (or the reply owed on the thread that arranged it). Every close is announced with an offer to reopen.

Incoming mail gets the same treatment in both places that judge it, the engine's screener and the morning briefing's summary: `situation.enrich_mail()` gives each email its thread so far, what memory holds about the sender and matter, and the people notes on the sender; `situation.in_motion()` adds the calendar and open commitments; both prompts share `MAIL_GUIDE` and his standing rules, so the two can't read the same email differently for lack of context.

`situation.know(about)` is the general form, exposed to the chat brain as the `look_up` tool: one call returns everything on a person, company, trip or topic from memory and earlier chats, mail both ways (90 days), the calendar (a month back, two ahead), commitments (open and recently closed) and the people notes. Mail and calendar are read live, never copied into memory: a copy goes stale, and memory holds only what exists nowhere else. This lookup is the base the background judges are meant to move onto, one monitor at a time.

Everything Aria says on her own judgment passes one gate ([ADR 0009](adr/0009-moments-and-one-gate.md)): judgment pings ("nudges" — chase, insight, moments) share a daily budget, and every judge reads the same standing rules and the same 48-hour log of what she has already told him, so a rule binds every monitor and none repeats another. Reports (reminders he set, digests, results of actions) always go through. Engine messages reach the live conversation too, so he can answer an offer with "yes".

| Monitor | What it does |
|---|---|
| `CommitmentMonitor` | Pings timed/recurring commitments at their moment; advances recurrences |
| `EmailDigestMonitor` | Deterministic bulk pre-filter → LLM-screens the rest (with his standing rules) → one evening digest, or an immediate ping when a rule asks for one; replies-owed become commitments (never calendar invites, payment requests or no-reply senders). Also closes deadline/promise commitments that new mail (inbound, or his own sent mail) proves done, and says so — only when the email contains a word unique to that commitment, so a proof can't land on the wrong item |
| `MomentMonitor` | Looks ahead at his calendar (`moments.py`): an hour before a meeting, the morning after it, the day before a flight, a few hours before a reservation. Does the legwork first (the email thread with the people involved, commitments to them, the time in his timezone), then speaks only if that adds to Google's own reminder. The event is the source of truth for time and join link; a link that appears in neither the event nor the mail drops the message |
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
smartalarm/              Apple Watch smart alarm (Swift watch app + Mac-tested core package)
main.py / morning_run.py Scheduled briefing (fail-loud delivery)
llm_router.py            Tiered, fallback-aware model factory
memory.py                Profile + ChromaDB semantic memory + memory tools
people.py                People model
instructions.py          Standing-instructions registry
engine.py                Proactivity engine + all monitors
situation.py             Mail and calendar around each open commitment (the task's other moving parts)
moments.py               Anticipation layer: calendar moments, touchpoints, pre-heads-up legwork
healthcheck.py           Self-diagnosis (doctor / tool / startup / monitor)
heartbeat.py             External dead-man's-switch ping
notify.py                Outbound Telegram notifications
email_backend.py         IMAP/SMTP backend (no-Google-Cloud email path)
email_filter.py          Deterministic bulk-mail pre-filter
report_generator.py      Daily briefing Markdown
nightly_compaction.py    Tier 1 → Tier 2 memory compaction
import_cold_storage.py   One-time import of retired cold-storage archives (ADR 0008)
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
evals/                   Behavioral eval suite: real agent, sandboxed tools/stores (python3 -m evals)
context/                 Implementation plan + task log
```
