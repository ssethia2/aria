# ADR 0002 — 3-tier OS-style memory architecture

**Status:** Accepted (2026-03)

## Context
Aria started with a static `profile.json`. To feel intelligent she needs to *learn continuously* from conversation, but the obvious approach — embed every fact into a vector DB the moment it's mentioned — adds embedding-model latency to every chat turn, and an unbounded vector store eventually dilutes retrieval quality and grows costly.

We want two things that pull in opposite directions: **instant conversational reflexes** and **deep long-term recall**.

## Decision
Model memory after an OS memory hierarchy, with three tiers and background jobs that move data downward as it ages.

1. **Tier 1 — Working memory (`daily_scratchpad.txt`).** `add_memory` appends raw facts to a plain text file. No embedding on the hot path → chat stays instant. `search_memory` reads this verbatim alongside Tier 2.
2. **Tier 2 — Short-term (`chroma_db/` via `nightly_compaction.py`).** A nightly cron job LLM-extracts clean semantic facts from the scratchpad, embeds them into ChromaDB, and wipes the scratchpad. This is Aria's standard semantic recall surface.
3. **Tier 3 — Cold storage (`cold_storage/` via `dynamic_consolidation.py`).** When the ChromaDB vector count crosses a threshold (~100), granular vectors are LLM-summarized into a long-form narrative file on disk, the originals are deleted, and a single **pointer vector** ("…read `cold_storage/<file>` via `read_cold_storage`") is left behind. The agent fetches deep context on demand with the `read_cold_storage` tool.

`profile.json` remains a separate Tier-0 static identity injected into every system prompt.

## Consequences
- **Latency where it matters:** the conversational path never blocks on embeddings.
- **Bounded vector store:** consolidation caps ChromaDB size and keeps retrieval sharp; deep memories survive as narratives + pointers.
- **Eventual consistency:** a fact told today isn't semantically searchable until the nightly compaction runs (it *is* visible via the scratchpad read in the meantime). The morning `run.sh` runs compaction before the briefing to narrow this window.
- **Lossy by design:** Tier 2→3 consolidation is an LLM summarization step — granular detail is intentionally compressed into narrative. Pointer + `read_cold_storage` recovers the narrative, not the original raw vectors (which are deleted).
- **Operational dependency:** the tiers only flow if `nightly_compaction.py` is actually scheduled (cron on the VM). Without it, everything piles up in Tier 1.
