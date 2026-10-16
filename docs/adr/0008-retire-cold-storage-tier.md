# ADR 0008 — Retire the cold-storage tier: one uncapped, dated semantic store

**Status:** Accepted (2026-10). Supersedes Tier 3 of [ADR 0002](0002-3tier-memory.md); Tiers 0–2 stand.

## Context
ADR 0002 capped ChromaDB at ~100 vectors. At the cap, `dynamic_consolidation.py` LLM-summarized
every fact into a narrative file in `cold_storage/`, deleted the facts, and left one **pointer
vector** ("…use `read_cold_storage` with filename X"). The premise was that an unbounded vector
store dilutes retrieval and grows costly.

In practice the pointer made old memories unreachable. `search_memory` returns the top few
matches, and the pointer is one generic sentence that resembles almost no real question. Tested
on 2026-10-16 with only 9 vectors in the store: for 5 questions whose answers were in the
latest archive, the pointer ranked in the top 3 for 2. As the store refills toward 100 the odds
fall further. The older archive was two hops away, its pointer folded into the newer narrative.
By then two consolidations (2026-06-20, 2026-10-10) had moved ~200 facts out of search, so Aria
had effectively forgotten everything learned before October 10.

The premise didn't hold either. ChromaDB's HNSW index serves millions of vectors; at a few facts
a day Aria reaches ~10k in years, and embedding is a one-time cost per fact.

## Decision
1. **No Tier 3.** Compaction only adds; nothing consolidates or deletes stored facts.
   `dynamic_consolidation.py` and the `read_cold_storage` tool are removed.
2. **Every fact is dated.** `memory.store_facts()` writes each fact with `added` (YYYY-MM-DD)
   and `source` metadata, and skips exact duplicates. `search_memory` shows the date
   ("…(noted 2026-10-14)") and its description tells the agent to trust the newer of two
   conflicting memories. Dates replace consolidation's one real job: superseding stale facts.
3. **`search_memory` returns 5 matches** (was 3), since the store now holds everything.
4. **One-time import.** `import_cold_storage.py` split both archives back into 201 standalone
   facts, dated by their archive, and removed the pointer. The archive files stay on disk as a
   backup; the script is idempotent.
5. The weekly reflection reads the week's new facts (`memory.recent_facts`) instead of the
   tail of the latest archive file.

## Consequences
- **Everything is searchable again.** After the import, questions answered only in the archives
  (e.g. the landlord, travel plans) return the right facts directly.
- **Imported facts carry the archive's date**, which means "known by then", not "learned then".
- **Stale facts persist alongside newer ones** (e.g. a past trip still reads as upcoming). The
  date on each result is the mitigation; if contradictions become a problem, the fix is to
  update or retire facts at write time, not to summarize them away.
- **Near-duplicates accumulate** (only exact duplicates are skipped). Cheap to tolerate at this
  scale; add semantic dedupe at write time if results start repeating themselves.
- **Embeddings are batched** with a wait-and-retry on the Gemini free tier's per-minute quota,
  which the per-fact calls tripped during the import.
- Found while doing this: compaction's line cleanup stripped digits from both ends of each fact,
  so facts ending in a date or number lost it ("…as of October 13," lost "2026."). Fixed with a
  shared parser (`memory.fact_lines`); facts already stored keep their truncation.
