# Goal Description
Develop a comprehensive, voice-activated personal AI assistant. The assistant will feature a modular architecture allowing it to learn new skills and manage context/memory over time.

> **How to read this doc:** Each section is a *phase* with a `(YYYY-MM · status)` tag. The doc accretes top-to-bottom as a changelog of design decisions — newer phases are lower in the file. For granular task status see [`task.md`](task.md); for what currently runs and how, see [`../README.md`](../README.md); for self-contained decision records see [`../docs/adr/`](../docs/adr/).

We are building this assistant iteratively. The **Pilot Skill** will be Email Management: the assistant will connect to Gmail, fetch recent emails, categorize them to identify junk/promotions, apply a "To Be Deleted" label to these junk emails for manual review, and generate summaries. This pilot will lay the foundation for the assistant's memory and reasoning capabilities.

## Phase 1 — Core Architecture & Email Pilot (2026-03 · done)
The architecture needs to support both the initial email skill and future voice interactions.

### Core Architecture Components
- **`main.py` (The Brain):** The central orchestrator that manages State, Memory, and Skills. It will handle the primary LLM interaction loop.
- **`memory.py` (The Memory):** Manages read/write operations for user preferences and context. We'll start with a local JSON profile and expand to a Vector Database (like ChromaDB) as we add more skills.
- **`skills/` (The Hands):** A directory for modular capabilities.
  - `skills/email_manager.py`: The pilot skill. Handles OAuth authentication and calling the Gmail API to fetch, summarize, and manage emails.
  - `skills/news_manager.py`: The second skill. Fetches daily newsletters (Morning Brew, NYT, CNN), extracts their full body content, and uses an LLM to aggregate news stories by topic, ensuring Morning Brew exclusive topics are preserved.
- **`interface/` (The Interface - Interactive Chat):**
  - We will build an interactive text REPL (Read-Eval-Print Loop) interface first (`interact.py` or similar). This will act as the primary way you converse with Aria locally.
  - The interface will use a LangChain Agent loop, allowing Aria to dynamically decide when to use her tools (e.g., searching memory, saving memory, fetching emails).
  - Later phases can upgrade this text interface to voice (`voice_in.py` and `voice_out.py`).
- **`requirements.txt`**: Project dependencies.

## Phase 2 — Memory Management & Interactive Agent (2026-03 · done)
To make Aria truly intelligent, we will upgrade her memory from a static JSON profile to a dynamic, continuous-learning Vector Database and introduce a conversational interface.
### Core Upgrades
1. **Interactive Agent Loop**: We will create a local chat script where you can converse with Aria. She will run as an autonomous agent with access to her tools.
2. **Vector Database (ChromaDB)**: We will implement ChromaDB in `memory.py` as Aria's long-term semantic memory. This allows Aria to store thousands of discrete facts, preferences, and events.
3. **Memory Tools**: We will create `save_memory` and `search_memory` functions that Aria can trigger on-demand during your conversation (e.g., if you say "Remind me I like my coffee black", she will autonomously call `save_memory`).
4. **Context Window Injection**: Every time you send a message in the chat, the system will first query ChromaDB for memories semantically similar to your message and inject them into Aria's system prompt, giving her perfect recall of past conversations.

### The 3-Tier Memory Architecture (2026-03 · done · see [ADR 0002](../docs/adr/0002-3tier-memory.md))
To optimize for both lightning-fast conversational response times and deep, long-term contextual recall, Aria will use a 3-Tier OS-style memory system:

1. **Tier 1: Working Memory (The Scratchpad)** 
   - During active conversations, we completely bypass the slow embedding model. Every message and extracted fact is simply appended to a raw, ultra-fast `daily_scratchpad.txt` (or local SQLite) log. 
   - This keeps Aria's conversational reflexes instantaneous. Her "short term" context is maintained purely by the active Langchain message loop.

2. **Tier 2: Short-Term Memory (`nightly_compaction.py`)** 
   - Every night via chronological `cron`, a script wakes up, reads the raw `daily_scratchpad.txt`, and uses the LLM to extract all the meaningful semantic facts.
   - It then generates embeddings for those facts and inserts them into our active ChromaDB vector database. The scratchpad is then wiped clean for the next day. 
   - This database acts as her standard semantic context window for the following days.

3. **Tier 3: Long-Term Cold Storage (`dynamic_consolidation.py`)**
   - A background monitor tracks the size of the ChromaDB collection. Once it breaches a threshold (e.g., > 100 vectors) or hits a monthly trigger, it groups related granular vectors.
   - The LLM summarizes these granular facts into comprehensive, long-form narratives (e.g., "Complete Travel History").
   - These narratives are written to disk in a `cold_storage/` directory, and a single, highly compressed "pointer" vector is left in ChromaDB (e.g., "Deep memory: Travel -> read `cold_storage/travel.txt`").
   - This simulates human deep recall latency: instant hit on the pointer, but requires a secondary tool (`read_cold_storage`) to read the deep context.

## Phase 3 — Netflix Household Automation (2026-03 · done)
To allow Aria to autonomously update the user's Netflix household based on an email sent to a secondary Gmail account, we need a specialized skill that handles discrete multi-account authentication and web navigation:
1. **Multi-Account Gmail API (`skills/netflix_manager.py`)**: We will use the existing Gmail OAuth logic but authorize a secondary token (`token_netflix.json`) specifically for the secondary Gmail account. The `update_netflix_household` tool will authenticate using this specific token to fetch the recent Netflix household update email.
2. **CTA Extraction**: Aria will parse the HTML body of the email to extract the secure "Update Netflix Household" CTA link.
3. **Browser Automation**: Since clicking the link often drops the user onto a Netflix webpage where an additional "Confirm" or "Update" button must be pressed (which may be protected by JavaScript or anti-bot measures), Aria will spin up a headless Browser Subagent to navigate to the link, locate the confirmation button, and physically click it to finalize the household update.
## Phase 4 — Cloud API & Instant Webhook (2026-03 · superseded — FastAPI server built; the Ngrok + Pub/Sub push items were replaced by the polling proactivity engine, see [ADR 0004](../docs/adr/0004-polling-engine-over-webhooks.md) and Phase 6)
Instead of relying on a slow 15-minute cron loop or talking to Aria exclusively through a local terminal, we will upgrade her into a true 24/7 Cloud API. 
1. **FastAPI Web Server**: We will wrap the core orchestration logic in a lightweight `aria_server.py` FastAPI app.
2. **Ngrok Tunneling**: We will use Ngrok to expose this local server to the public internet securely (and eventually deploy it to the GCP VM).
3. **Gmail Push Webhooks (Pub/Sub)**: With a public URL, we can configure Google Cloud Pub/Sub to instantly send a JSON webhook to Aria the absolute second a Netflix email hits the secondary inbox. Aria will intercept this webhook, parse the email, and trigger the Browser Subagent with 0 minutes of delay.
This perfectly positions Aria for the future: with a public URL, you suddenly have the foundation to text her, ping her from iOS Shortcuts, or talk to her remotely from your phone.

## Phase 5 — Autonomous Day Planning / Agentic Agenda (2026-03 · done)
Because Aria operates via a text-based REPL interface when the user is at the computer, she cannot proactively push notifications to the user out of nowhere. To solve the problem of reminding the user and planning their day, we will use an asynchronous push model:
1. **Calendar/ToDo Skill**: We will create a `skills/calendar_manager.py` skill with a local SQLite database (`aria_calendar.db`). Aria can use the `add_reminder` tool during daily chat (e.g., "Remind me to call Mom tomorrow").
2. **Morning Briefing Payload (`main.py`)**: Our current architecture uses `main.py` running on a VM cron job at 8:00 AM every day to email the user a Markdown summary of their inbox. We will upgrade this payload. 
3. **The Agentic Agenda**: When `main.py` wakes up in the morning, it will query the `aria_calendar.db` for today's reminders. It will combine the user's Inbox Zero summary, Morning News brief, and Today's Reminders into one comprehensive "Morning Briefing" email. 
This allows Aria to proactively "plan the day" and remind the user of events securely in their inbox when they wake up, circumventing the limitations of the local REPL interface.
## Cross-cutting — Cloud Deployment Strategy / GCP Compute Engine (2026-03 · ongoing · see [ADR 0003](../docs/adr/0003-gmail-scope-restriction.md))
To run the assistant dynamically as a persistent "true agent", we will deploy it to a Google Cloud Platform (GCP) Compute Engine instance.

### Architecture & Persistence
1. **Persistent Execution Environment**: A Compute Engine instance (e.g., `e2-micro`) running Debian or Ubuntu will host the code 24/7. This guarantees that local databases, your `profile.json` memory, and the Gmail `token.json` remain completely intact over time.
2. **Access Management & Defense-in-Depth (Service Accounts)**: 
   - The Compute Engine instance will be assigned a GCP Service Account.
   - The assistant will require a read-only list of approved email addresses (`allowlist.json`). This file will be stored in a private **Google Cloud Storage (GCS) Bucket**.
   - The Service Account assigned to the instance will explicitly be granted *only* the `Storage Object Viewer` (Read-Only) role for this specific bucket. This ensures that even if the agent goes rogue, it is physically impossible for it to modify the allowlist.
3. **Delivery (Email Sending)**: Since you won't be logging into the server directly to read the Markdown file, the assistant will use the Gmail API to **email the Daily Summary directly to your inbox**.
4. **Setup & Initialization**: We will provide a shell script to clone the code onto the Compute Engine instance, install the Python dependencies into a virtual environment, and complete the one-time OAuth authentication.

## Phase 6 — Ubiquity, Durability & Proactivity (2026-06 · done)
Reorientation around the "Jarvis" goal: the defining features are not more skills but the connective tissue — being reachable anywhere, remembering across restarts, failing loud, and *initiating* contact. Hosting moved local-first after the GCP bill proved unjustifiable (see ADR 0004 context).

1. **Shared agent core (`agent_core.py`)**: one definition of tools + system prompt; all interfaces are thin wrappers. The system prompt (with live date/time) is injected per model call via middleware, never stored in state.
2. **Telegram interface (`telegram_bot.py`)**: long-polling Bot API — phone-reachable with zero public infrastructure. Chat-id allowlist required; setup mode helps discover the id.
3. **Durable conversations**: LangGraph SQLite checkpointer (`aria_checkpoints.db`); each Telegram chat and the REPL get persistent threads that survive restarts.
4. **Resident service (launchd)**: `launchd/com.aria.telegram-bot.plist` (login + crash restart) and `launchd/com.aria.morning-briefing.plist` (08:00 daily; fires on wake if asleep — replaces the GCP cron).
5. **Briefing → Telegram + fail-loud**: `main.py` delivers via Telegram (primary) and email; any failure telegrams the error instead of silently skipping (`notify.py`).
6. **Proactivity engine (`engine.py`)**: pluggable polling monitors (reminders due, important-email screen, Netflix auto-handling) with quiet hours and a morning digest queue. See ADR 0004.
7. **Tests (`tests/`)**: offline unit suite covering the allowlist security contract, report rendering, the notifier, and engine mechanics.

## Phase 7 — Commitment Keeper (2026-06 · done)
From the needs interview (2026-06-10): what actually slips are replies owed, deadlines,
people dates, and verbal promises that never enter any system. Aria-on-Telegram is the
lowest-friction capture device — telling Aria IS the system.

1. **Commitment store** (`skills/commitment_manager.py`): typed (`reply_owed` / `deadline` / `people_date` / `promise`), who/due-date/due-time, yearly recurrence (birthdays roll forward on completion), legacy reminders migrated in.
2. **Chat capture**: agent tools (add/list/complete/drop) + a system-prompt core duty to capture commitments mentioned even in passing.
3. **Engine**: `CommitmentMonitor` pings timed items at their moment; date-only items surface in the briefing/digest only. `EmailDigestMonitor` replaces instant pings with one ~18:00 digest and feeds replies-owed into the store. `ChaseMonitor` runs daytime LLM judgment over open commitments — overdue/aging/stale items get at most ONE warm nudge a day, silence the default.
4. **Briefing** leads with due/overdue + 7-day upcoming commitments.

## Phase 8 — Human Touch (planned)
Goal: Aria should feel like someone who *knows you*, not a database with a chat interface.
The girlfriend incident (2026-06): asked to remember something about "my girlfriend", Aria
stored the string without asking her name — a human assistant would have felt the gap.

1. **Relational curiosity (done, prompt-level)**: when an unnamed person enters conversation, check memory; if unknown, ask the name naturally (ONE question), save it, use it thereafter. Same pattern for any missing key detail (date-less commitment, time-less event).
2. **People model (next)**: promote people from loose semantic facts to first-class entities — name, relation, dates, preferences, open threads ("interview next week"). Commitments' `who` field links to it; memory recall groups by person; briefings can say "Priya's birthday is in 3 days" with gift context.
3. **Conversational callbacks**: surface saved "circle back" items in later conversations ("how did the interview go?") — likely a ChaseMonitor-style judgment pass over recent memories.
4. **Tone & continuity**: greet by time of day, reference shared history sparingly, never re-ask known facts (the cardinal sin of fake-human assistants).

## Verification Plan
### Automated Tests
- Test core LLM routing and memory injection.
- Test the Email Manager skill in isolation (fetching and categorizing synthetic/real emails).

### Manual Verification
- Run the assistant loop in the terminal.
- Verify the assistant successfully executes the email summary "skill" when requested.
- Verify that the assistant successfully remembers a fact told to it via the terminal interface.
