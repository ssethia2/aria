# ADR 0010 — Conversations are memory

**Status:** Accepted (2026-11). Extends Tier 1 of [ADR 0002](0002-3tier-memory.md) and
[ADR 0008](0008-retire-cold-storage-tier.md); Tier 2 stands.

## Context
Only two things reached long-term memory: facts the agent chose to save with `add_memory`, and
engine notifications. Conversations themselves didn't. A conversation lived in the day's SDK
session, then in a 40-entry mirror. The mirror bridges only the last ten exchanges into a new
session, and engine pings crowd them out.

In use: he spent an evening building a trip itinerary with her, pasting flight details in by
hand. She saved none of it. Two days later, at 4 AM in a new session, she didn't know the
trip. She guessed at which booking he meant, then claimed a booking matched when the email
she'd just opened was unreadable. Separately, the email reader turned HTML-only booking mail
into CSS and entity padding, so she couldn't have looked the trip up either.

## Decision
1. Every exchange with the owner is appended to `daily_conversation.jsonl` (gitignored) as
   it lands in the mirror. That covers the subscription turn, the API fallback and the fast
   path. Engine notifications are excluded; they already reach the scratchpad.
2. Nightly compaction reads it alongside the scratchpad. It keeps what he told her or what
   they settled together: plans, bookings with their dates, times and codes (one fact per
   booking or leg), decisions, preferences and corrections. It discards the back-and-forth.
   It never stores a password, PIN, card number or one-time code. Both files are wiped only
   after the facts are stored.
3. Until compaction runs, `search_memory` also returns the logged exchanges that share the
   most words with the query, so a new session can reach last night's conversation.
4. She doesn't have to decide to remember. Given the trip in memory, an eval showed she
   still went straight to email, every time. So each substantive message (two or more
   content words; not "yes please") reaches the subscription brain with `memory.recall()`
   prepended: dated semantic matches plus matching logged exchanges, inside
   `<what_you_remember>`.
5. Test and eval fixtures point the log at temp files, and the eval sandbox serves recall
   from the scenario's memory. Eval chats must never become his memories, and evals never
   read his real store.

## Consequences
- What he says in chat outlives the session without the agent having to decide to save it.
- Each substantive turn costs one embedding lookup and a few hundred tokens of recall.
- One compaction call grows by the day's conversation, capped at 60K characters with the
  newest first. That's cents a day on the standard tier.
- Every owner conversation now sits on disk until compaction, next to the scratchpad. It
  is the same exposure the SDK transcripts already have.
- Compaction can still keep a fact she got wrong in the moment. The prompt asks it to
  prefer his corrections, and dated facts let the newer one win.
