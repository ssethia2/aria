# ADR 0001 — Claude → Gemini model fallback router

**Status:** Accepted (2026-03)

## Context
Every skill (email triage, news synthesis, memory compaction) and the interactive agent need an LLM. Calling provider SDKs directly in each module would (a) scatter model-name and API-key handling across the codebase, and (b) leave Aria dead in the water whenever a single provider has an outage, rate-limits us, or returns a 404 because our API tier can't access a given model.

We also observed that model availability is tier-dependent: the newest Claude models sometimes 404 on lower API tiers, while older or alternate-provider models stay reachable.

## Decision
Centralize all model instantiation in a single factory, `llm_router.py::get_llm()`, that returns one LangChain chat model with a built-in fallback chain:

**Claude 3 Opus → Claude 3.5 Sonnet → Gemini 2.5 Flash.**

The chain is assembled with LangChain's `.with_fallbacks()`, and models that fail to *initialize* are dropped from the chain dynamically, so a missing key for one provider doesn't break the others. Every module obtains its model via `get_llm()` rather than importing a provider SDK directly.

## Consequences
- **Resilience:** a provider outage or tier limit degrades gracefully to the next model instead of failing the run.
- **One place to change models:** upgrading to a newer Claude/Gemini model is a single-file edit. The pinned `claude-3-opus-20240229` / `claude-3-5-sonnet-20240620` choices live here with a comment explaining the tier constraint — revisit when our API tier changes.
- **Mixed capabilities:** fallback models differ in quality and tool-calling fidelity, so output can vary depending on which model answered. Acceptable for a personal assistant; would need per-skill model pinning if quality variance ever matters.
- **Embeddings are separate:** semantic-memory embeddings use Gemini (`models/gemini-embedding-001`) directly in `memory.py` and are *not* part of this fallback chain. Switching embedding providers would re-index ChromaDB — out of scope here.
