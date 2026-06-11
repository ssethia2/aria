# Aria — AI Personal Assistant

> **Aria** = *Aria Responds Intelligently Always.* A modular, memory-aware personal AI assistant that triages your inbox, briefs you on the news, plans your day, and runs autonomous chores — delivered as a morning email and an interactive chat.

Aria is built on **LangChain / LangGraph** with a resilient **Claude → Gemini** model router. It connects to Gmail, learns about you over time through a 3-tier memory system, and exposes its skills both as a daily batch job and as a conversational agent.

For the *why* behind the architecture and the phased roadmap, see [`context/implementation_plan.md`](context/implementation_plan.md). For task status, see [`context/task.md`](context/task.md). This README covers *what exists today and how to run it*.

---

## What Aria does

| Capability | Skill / Module | Trigger |
|---|---|---|
| Inbox triage — classify recent mail as IMPORTANT / NEWS / JUNK and label junk `To Be Deleted` | `skills/email_manager.py` | morning job + chat |
| Bulk inbox cleanup across the whole inbox | `clean_inbox.py` | manual |
| Daily news brief — aggregate Morning Brew / NYT / CNN newsletters into deduped topics | `skills/news_manager.py` | morning job + chat |
| Commitment keeping — capture promises/deadlines/birthdays/replies-owed from chat, timed pings, nothing slips | `skills/commitment_manager.py` | chat + engine + briefing |
| Netflix Household update via headless browser | `skills/netflix_manager.py` | chat + webhook |
| Long-term semantic memory + recall | `memory.py` | every chat turn |
| Proactive monitoring — due-reminder pings, important-email alerts, autonomous Netflix handling | `engine.py` | background thread in the bot |
| Morning briefing email (inbox + news + reminders) | `main.py` → `report_generator.py` | cron @ 08:00 |
| Always-on webhook server | `aria_server.py` | Gmail Pub/Sub push |

---

## Architecture at a glance

```
                        ┌─────────────────┐
        chat ──────────▶│  interact.py    │  LangGraph ReAct agent (REPL)
                        │  (the agent)    │  tools: memory, email, news,
                        └────────┬────────┘         netflix, reminders
                                 │
   cron 08:00 ──▶ main.py ──┐    │
                            ▼    ▼
                  ┌──────────────────────┐
                  │   llm_router.py      │  Opus → Sonnet → Gemini fallback
                  └──────────┬───────────┘
                             │
       ┌─────────────┬───────┴────────┬──────────────┬───────────────┐
       ▼             ▼                ▼              ▼               ▼
  email_manager  news_manager   calendar_manager  netflix_manager  memory.py
   (Gmail API)    (Gmail API)     (SQLite)         (Gmail+Playwright) (ChromaDB)
       │                                                              │
       ▼                                                              ▼
  report_generator.py ──▶ reports/*.md ──▶ emailed to you      3-tier memory:
                                                                scratchpad → Chroma → cold_storage

  aria_server.py (FastAPI) ◀── Gmail Pub/Sub push ──▶ triggers update_netflix_household
```

### Interfaces (entry points)

All three share one agent definition in `agent_core.py` (tools + system prompt), so they stay thin and consistent. Conversations are checkpointed to SQLite (`aria_checkpoints.db`) via LangGraph, so chat history survives restarts — each Telegram chat and the local REPL get their own persistent thread.

- **`telegram_bot.py`** — **chat from your phone**. Long-polls the Telegram Bot API (no public endpoint, port, or server needed) and runs the agent. Reachable anywhere; runs even from your laptop. See [Chat with Aria on Telegram](#chat-with-aria-on-telegram).
- **`interact.py`** — the **local REPL**. Same agent, in your terminal. Run `python3 interact.py`.
- **`main.py`** — the **batch morning job**. Runs the email summary, news brief, and today's reminders, builds a Markdown report, and delivers it — Aria messages you on Telegram (primary) and emails a copy. Fails loud: errors trigger a Telegram alert instead of a silent miss. Scheduled via `run.sh` (cron or the provided launchd agent).

### The model router (`llm_router.py`)
A single `get_llm()` returns a LangChain chat model with a built-in fallback chain: **Claude 3 Opus → Claude 3.5 Sonnet → Gemini 2.5 Flash**. Every skill and the agent loop go through it, so a single API outage or tier limit degrades gracefully instead of breaking.

### The 3-tier memory (`memory.py` + compaction scripts)
1. **Tier 1 — Working memory:** `add_memory` appends raw facts to `daily_scratchpad.txt` (instant, no embedding).
2. **Tier 2 — Short-term:** `nightly_compaction.py` runs nightly, LLM-extracts facts from the scratchpad, embeds them into **ChromaDB** (`chroma_db/`), and wipes the scratchpad.
3. **Tier 3 — Cold storage:** when ChromaDB exceeds a threshold, `dynamic_consolidation.py` summarizes granular vectors into a long-form narrative file in `cold_storage/`, replacing them with a single "pointer" vector. The agent reads these on demand via `read_cold_storage`.

`search_memory` reads Tier 1 + Tier 2 together; `profile.json` holds static core identity injected into every system prompt.

---

## Setup

### Prerequisites
- Python 3.10+
- A Google Cloud project with the **Gmail API** enabled and OAuth **Desktop** credentials downloaded as `credentials.json`
- An **Anthropic** API key and a **Gemini** API key

### 1. Install
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # only needed for the Netflix skill
```

### 2. Configure secrets
Create `.env` in the project root:
```bash
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
ALLOWLIST_BUCKET_NAME=your-gcs-bucket   # optional; falls back to a hardcoded allowlist
```
Place your Google OAuth client at `credentials.json`. The first run opens a browser to authorize Gmail and writes `token.json`.

Optionally edit `profile.json` to seed Aria with who you are:
```json
{ "name": "Satvik", "preferences": { "beverages": ["Coffee"] } }
```

> **Security note:** Aria can only *send* email to addresses on an allowlist. In production this lives read-only in a GCS bucket (`ALLOWLIST_BUCKET_NAME` → `allowlist.json`); locally it defaults to `["satviksethia@gmail.com"]`. OAuth scopes are deliberately limited to `gmail.modify` + `gmail.send` — Aria **cannot permanently delete** mail, only label it `To Be Deleted`.

### 3. Run
```bash
# Chat from your phone (recommended — see Telegram setup below)
python3 telegram_bot.py

# Local terminal chat
python3 interact.py

# One-off morning briefing (also runs nightly compaction first)
./run.sh
```

### Chat with Aria on Telegram
The lowest-friction way to reach Aria, and zero hosting cost — long-polling means no public URL or open ports.

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Run `python3 telegram_bot.py` and message your bot. While no allowlist is set it runs in **setup mode** and replies with your chat id.
3. Put that id in `.env` as `TELEGRAM_ALLOWED_CHAT_ID` (comma-separate for multiple) and restart. Only allowlisted chats are served thereafter — **required**, since anyone who finds the bot can message it and Aria can read your email.

### Proactivity engine
While the bot runs, `engine.py` polls in a background thread and Aria reaches out unprompted:
- **Commitments** (every 2 min) — time-specific commitments ping at their moment; date-only ones surface in the briefing/digest instead of buzzing randomly
- **Email digest** (screens every 15 min, delivers ~18:00) — new mail is LLM-screened and accumulated into one evening digest; replies you owe are auto-tracked as commitments
- **Netflix household** (every 30 s) — a fresh update email triggers the browser automation within ~a minute; the post-click page is verified and you get an honest outcome report

During **quiet hours (23:00–08:00)** Aria still *acts* but queues the pings, sending one "🌅 While you were away" digest in the morning. State lives in `engine_state.json`; disable entirely with `ARIA_ENGINE_DISABLED=1`. Add a monitor by subclassing `Monitor` in `engine.py` and registering it in `default_engine()` (see ADR 0004).

### Run Aria as a resident service (macOS launchd)
`launchd/` contains two agents: `com.aria.telegram-bot` (starts the bot at login, auto-restarts on crash) and `com.aria.morning-briefing` (runs `run.sh` daily at 08:00; if the laptop was asleep, it fires once on wake). Install with:
```bash
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aria.telegram-bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aria.morning-briefing.plist
```
Logs land in `logs/`. To stop: `launchctl bootout gui/$(id -u)/com.aria.telegram-bot`. Don't also run `telegram_bot.py` manually — two pollers on one token cause Telegram 409 conflicts.

---

## Optional capabilities

### Netflix Household automation
Uses a **second** Gmail account (the one that receives Netflix mail):
```bash
python3 auth_netflix.py        # authorize secondary account → token_netflix.json
```
`update_netflix_household` then finds the latest household email, extracts the CTA link, and uses a headless Playwright browser to click the confirm button.

### Instant webhooks (always-on)
```bash
python3 aria_server.py                       # FastAPI on :8000
python3 setup_gmail_watch.py                  # register Gmail → Pub/Sub push (edit PROJECT_ID/TOPIC first)
```
Gmail Pub/Sub push notifications hit `POST /webhook/gmail` and trigger the Netflix tool instantly. (Ngrok tunneling / GCP deploy is roadmap — see the plan doc.)

### Cloud deployment
`setup_gcp.sh` bootstraps a GCP Compute Engine VM (Python, venv, deps) and prints the cron line for the 08:00 morning briefing. Transfer `.env`, `credentials.json`, and `profile.json` to the VM, run `main.py` once to complete OAuth, then add the cron job.

---

## Repository layout

```
agent_core.py            Shared agent definition (tools + system prompt) used by all interfaces
telegram_bot.py          Telegram chat interface (long-polling; phone-reachable)
interact.py              Local REPL interface (terminal)
main.py                  Batch morning-briefing orchestrator (cron entry point)
llm_router.py            Unified Claude→Gemini fallback model factory
memory.py                Profile + ChromaDB semantic memory + memory tools
report_generator.py      Builds the daily Markdown summary
notify.py                Outbound Telegram notifications (briefing delivery, failure alerts)
engine.py                Proactivity engine — polling monitors, quiet hours, morning digest
aria_server.py           FastAPI webhook server (Gmail Pub/Sub)
clean_inbox.py           Standalone bulk inbox cleaner
launchd/                 macOS launchd agents (resident bot + 08:00 briefing)
tests/                   Offline unit tests (python3 -m unittest discover tests)
nightly_compaction.py    Tier 1 → Tier 2 memory compaction (nightly cron)
dynamic_consolidation.py Tier 2 → Tier 3 cold-storage consolidation
auth_netflix.py          One-time OAuth for the secondary Netflix Gmail account
setup_gmail_watch.py     Registers Gmail push notifications to Pub/Sub
setup_gcp.sh             GCP Compute Engine bootstrap
run.sh                   venv activate → compaction → main.py (cron wrapper)
skills/
  email_manager.py       Gmail auth, fetch, classify, label, send
  news_manager.py        Newsletter fetch + topic aggregation
  commitment_manager.py  Commitment store + capture tools (Aria's core loop)
  calendar_manager.py    LEGACY reminders (migrated into commitments)
  netflix_manager.py     Netflix household browser automation
context/
  implementation_plan.md Design rationale + phased roadmap
  task.md                Phase-by-phase task checklist

# Generated / gitignored at runtime:
#   token*.json, profile.json, .env, credentials.json
#   chroma_db/, cold_storage/, reports/, daily_scratchpad.txt
#   aria_calendar.db, aria_checkpoints.db
```

---

## Environment & config reference

| Variable / file | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude (primary + fallback models) |
| `GEMINI_API_KEY` | Gemini chat model + embeddings |
| `ALLOWLIST_BUCKET_NAME` | GCS bucket holding the send allowlist (optional) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather (for `telegram_bot.py`) |
| `TELEGRAM_ALLOWED_CHAT_ID` | Comma-separated chat ids allowed to use the bot |
| `credentials.json` | Google OAuth client (Desktop) |
| `token.json` / `token_netflix.json` | Generated OAuth tokens (primary / Netflix account) |
| `profile.json` | Static core identity injected into prompts |
| `allow.json` | Local email send allowlist (mirrors the GCS `allowlist.json`) |

---

## Status & roadmap

Current phase work is tracked in [`context/task.md`](context/task.md). Open items at last update: Ngrok tunneling, full Pub/Sub push wiring, and connecting the webhook end-to-end.
