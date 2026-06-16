# Aria — a personal AI assistant that actually remembers you

> *Aria Responds Intelligently Always.* A self-hosted, memory-aware personal assistant you talk to from your phone — it keeps your commitments, triages your inbox, manages your calendar, and reaches out *unprompted* when something's worth your attention. Your hardware, your keys, your data.

Most "AI assistants" are a chat box with a system prompt that cannot handle large context windows inexpensively, and push you to start new conversations every time. Context is not reliably available across chats, and relevant information is buried in a sea of useless tokens. Aria is built around the parts that make an assistant feel like it *knows you and works for you*: a layered memory that remembers the information that matters and learns over time, a model of the people in your life, rules you can set in plain language, and a background engine that exercises judgment about when to speak. You message it on Telegram — by text or voice — and it lives on a machine you own. All your personal data is stored on your device, but will be used by the underlying LLM. 

```
You:  btw I told my dad I'd help him set up his new phone this weekend,
      and it's my girlfriend Priya's birthday on June 28
Aria: Both tracked 📱🎂 — helping your dad this Saturday, and Priya's birthday
      on the 28th (I'll remind you yearly). What are her favorite flowers, by the way?

         …later, unprompted…
Aria: 💡 Heads-up: heavy rain Saturday morning, right when you're set to play
      tennis with Dev at 10 — might be worth checking with him about an indoor court.
```

### 🎙️ Voice

Because let's be real — aren't we done being disappointed by Siri? You can *talk* to Aria, not just type: an on-device voice mode (local Whisper, fully private), a realtime conversational mode over Gemini Live you can **interrupt mid-sentence** (true barge-in), and a browser/phone client you add to your home screen like a native app. The snappy voice layer hands anything real — memory, email, calendar — to the same brain.

---

## ✨ What makes it interesting

### 🧠 A memory that's actually architected
Aria doesn't dump everything into one vector store and hope. It uses a **three-tier, OS-inspired memory hierarchy** that trades off speed and depth the way a computer trades registers, RAM, and disk:

- **Tier 1 — Working memory.** New facts append to a plain-text scratchpad instantly, with zero embedding latency, so conversation stays fast.
- **Tier 2 — Semantic memory.** Each night an LLM distills the scratchpad into clean, durable facts and embeds them into a vector store (ChromaDB) for associative recall — and it *filters*: durable facts about you are kept, operational noise ("sent a digest", "fired a reminder") is discarded so the store stays sharp.
- **Tier 3 — Cold storage.** When semantic memory grows large, related facts are consolidated into long-form narratives on disk, leaving a single "pointer" vector behind — human-like deep recall that's instant to *find* and loaded on demand.

The result is memory that gets *better* over time instead of bloating.

### 👥 It models the people in your life
People aren't strings — they're **first-class entities** with names, relationships, birthdays, and remembered details. Mention "my girlfriend" and Aria doesn't know who that is, it asks her name *once*, saves it, and never asks again. Tell it a birthday and it silently becomes a recurring yearly reminder. This is the difference between an assistant that *stores text about you* and one that *knows your world*.

### 📋 You program it in plain English
Tell Aria *"from now on, put work events only on my personal calendar"* and it saves a **standing instruction** that's enforced on every future turn — a rule it manages itself (add, refine, revoke) and can't forget. Persistent policy lives where it belongs: always in force, not dependent on it happening to recall a memory.

### 🫀 A proactivity engine with judgment
A background engine doesn't just fire timers — twice a day it looks **across your calendar, commitments, and the weather together** and decides whether there's *one* genuinely useful thing to say (a conflict, a good window to clear an aging task, a plan the weather will ruin). Most of the time it correctly says nothing. That restraint is the point: it earns the right to interrupt you.

### 🤝 It keeps your commitments so you don't have to
Mention a promise, a deadline, a reply you owe, or a recurring task — even in passing — and it's captured. Time-specific things ping at their moment; aging ones get a single warm nudge. Telling Aria *is* the system of record.

---

## 🧰 Everything it can do

| Area | What it does |
|---|---|
| **Commitments** | Capture promises/deadlines/replies-owed/birthdays from chat; timed & recurring reminders; judgment-based chasing so nothing slips |
| **Email** | Triage, a once-daily digest (not noisy pings), and reply *drafts* you approve — via Gmail API **or** plain IMAP/SMTP (no Google Cloud project needed) |
| **Calendar** | Create / edit / delete Google Calendar events, with per-user rules (e.g. mirror to a shared calendar, color-coded) |
| **Notes & lists** | A full notes system, plus a grocery list that builds itself from a recipe or dish name |
| **Research & web** | Web search + page reading; an agentic browser that explores a task (e.g. fill a form, find options) and reports back — never spending your money |
| **Smart home** | Control Matter devices via Home Assistant |
| **Knows your day** | Weather, package tracking, and a morning briefing delivered to your phone |
| **Voice** | Send a voice note, get a voice note back — transcribed locally (Whisper) |

Plus the invisible parts: durable conversations that survive restarts, a self-diagnosing health system, and a cost-tiered model router. *(There's even a gratuitous skill that auto-confirms Netflix Household emails via headless browser — every project needs one trophy of over-engineering.)*

---

## 🏗️ How it's built

A handful of thin interfaces — **Telegram** (text + voice notes), a **terminal REPL**, a **scheduled briefing**, and **realtime voice** (on-device, Gemini Live, and a browser/phone app) — all share one agent definition. The agent calls modular **skills**; a background **engine** runs polling monitors. Everything routes its LLM calls through one tiered, fallback-aware router.

```
  Telegram · REPL · briefing · voice (local · live · web)
                │
          agent_core.py ── tiered LLM router ──▶ Opus / Sonnet / Gemini·Haiku
                │                                  (per-task, prompt-cached)
     ┌──────────┼───────────────────────────┐
     ▼          ▼                            ▼
  skills/   3-tier memory + people     engine.py (background)
  (email,   (scratchpad→Chroma→cold,   commitments · email digest ·
  calendar,  durable conversations      insight · health · heartbeat
  notes,     via SQLite checkpoints)    · …
  browser…)
```

Design rationale and the decision record live in [`docs/`](docs/):
- **[docs/architecture.md](docs/architecture.md)** — memory, agent core, router, engine, repo map
- **[docs/setup.md](docs/setup.md)** — full setup & configuration reference
- **[docs/adr/](docs/adr/)** — architecture decision records (why these choices)
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — conventions, the "add a skill" recipe, and the contributor CLA

Offline test suite: `python3 -m unittest discover tests` (160+ tests, all network/LLM mocked).

---

## 🚀 Quick start

Runs on your own machine with your own keys.

```bash
git clone <this repo> && cd aria
bash setup.sh        # venv, deps, guided .env wizard, health check
venv/bin/python3 telegram_bot.py
```

Message your new bot — it replies with your chat id; paste that into `.env` and restart. That's a working assistant (chat, memory, commitments, notes, weather, research). **Only three secrets are required** — a Telegram bot token, an Anthropic API key, and your chat id; every other feature (Gmail/Calendar, smart home, semantic memory) layers in cleanly when you add its config. Full details in [docs/setup.md](docs/setup.md).

For 24/7 operation, host it on a Raspberry Pi or any always-on Linux box — see [docs/pi-migration.md](docs/pi-migration.md).

---

## 📜 License & commercial use

Licensed under the **GNU AGPL-3.0** (see [LICENSE](LICENSE)) — you're free to use, study, modify, and self-host it, and any modified version you run as a network service must publish its source.

**Commercial licensing:** the AGPL's source-disclosure terms are unsuitable for closed/commercial products. Aria is **dual-licensed** — for a commercial license that lifts the AGPL obligations, contact the author. © Satvik Sethia.

**Contributing:** contributions are welcome under a [Contributor License Agreement](CLA.md). The CLA lets the author offer your contribution under both the AGPL and commercial licenses (keeping the dual-licensing path clean) while you keep ownership of your work — see [CONTRIBUTING.md](CONTRIBUTING.md).
