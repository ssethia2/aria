"""Memory layer + LangChain memory tools (see docs/adr/0002, and 0008 which retired Tier 3).

Tier 0: static profile.json. Tier 1: daily_scratchpad.txt (add_memory).
Tier 2: ChromaDB semantic store (search_memory) — every durable fact, dated, uncapped.
nightly_compaction.py moves Tier 1 into Tier 2 via store_facts().
"""
import datetime
import json
import os
import re
import time
import uuid
import chromadb
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from dotenv import load_dotenv

from tenant import (is_guest, get_current_user, safe_id, reset_current_user,
                    scope_from_config as _tenant_scope)

load_dotenv()

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "profile.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "chroma_db")
SCRATCHPAD_PATH = os.path.join(os.path.dirname(__file__), "daily_scratchpad.txt")

# --- Layer 1: Core Identity (Static Profile) ---

def load_profile():
    # Guests have no static owner profile — their personalization lives in their own memory.
    if is_guest():
        return {}
    if not os.path.exists(PROFILE_PATH):
        return {}
    with open(PROFILE_PATH, "r") as f:
        return json.load(f)

def save_profile(data):
    with open(PROFILE_PATH, "w") as f:
        json.dump(data, f, indent=4)
        
def update_profile(key, value):
    """Updates a specific key in the user's profile memory."""
    profile = load_profile()
    profile[key] = value
    save_profile(profile)
    print(f"✅ Basic Memory Updated: {key} -> {value}")


# Tag the proactivity engine uses when logging its actions to the scratchpad.
# Shared so engine.py writes it and the compaction filter's DISCARD rule matches on it.
ACTION_LOG_PREFIX = "[Aria proactive action"

# --- Layer 2: Semantic Memory (ChromaDB) ---

# Initialize ChromaDB correctly on first load
chroma_client = None
try:
    chroma_client = chromadb.PersistentClient(path=DB_PATH)
    collection = chroma_client.get_or_create_collection(name="aria_memory")
    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=os.getenv("GEMINI_API_KEY")
    )
except Exception as e:
    print(f"⚠️ Warning: Failed to initialize ChromaDB. Semantic memory disabled. Error: {e}")
    collection = None
    embeddings = None


def _guest_collection():
    """The current guest's OWN ChromaDB collection — isolated per user (`mem_<user>`)."""
    if chroma_client is None:
        return None
    return chroma_client.get_or_create_collection(name=f"mem_{safe_id(get_current_user())}")

def _add_memory(fact: str) -> str:
    if is_guest():
        # Guests write straight into their own isolated collection (no shared scratchpad,
        # no compaction job — those are owner-only).
        col = _guest_collection()
        if col is None or embeddings is None:
            return "Memory isn't available right now."
        try:
            col.add(ids=[uuid.uuid4().hex], documents=[fact],
                    embeddings=[embeddings.embed_query(fact)])
            return f"Got it — I'll remember that: {fact}"
        except Exception as e:
            return f"Failed to add memory: {str(e)}"
    try:
        with open(SCRATCHPAD_PATH, "a") as f:
            f.write(fact + "\n")
        print(f"📝 Working Memory Added: {fact}")
        return f"Successfully remembered in working memory: {fact}"
    except Exception as e:
        return f"Failed to add memory: {str(e)}"


@tool
def add_memory(fact: str, config: RunnableConfig = None) -> str:
    """Use this tool to add a new memory, fact, preference, or event about the user.
    Provide a clear, detailed sentence (e.g., 'Satvik hates newsletters', 'Satvik is traveling to NY in July').
    """
    tok, refusal = _tenant_scope(config)
    if refusal:
        return refusal
    try:
        return _add_memory(fact)
    finally:
        if tok:
            reset_current_user(tok)

def fact_lines(text: str) -> list:
    """An LLM's one-fact-per-line output as a list: leading bullets/numbering and headings
    removed, nothing trimmed from the END (facts often end in a date or number)."""
    facts = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", line).strip()
        if line and not line.startswith("#"):
            facts.append(line)
    return facts


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


_EMBED_BATCH = 50


def _embed_all(texts: list) -> list:
    """Embed in batches, waiting out the Gemini free tier's per-minute quota when it bites."""
    vectors = []
    for i in range(0, len(texts), _EMBED_BATCH):
        chunk = texts[i:i + _EMBED_BATCH]
        for attempt in range(4):
            try:
                # RETRIEVAL_QUERY: how every stored vector has always been made (embed_query).
                vectors += embeddings.embed_documents(chunk, task_type="RETRIEVAL_QUERY")
                break
            except Exception as e:
                if attempt == 3 or "RESOURCE_EXHAUSTED" not in str(e):
                    raise
                print("Embedding quota hit — waiting a minute…")
                time.sleep(61)
    return vectors


def store_facts(facts, added: str, source: str = "compaction") -> int:
    """Embed owner facts into ChromaDB, each tagged with the date it was learned (`added`,
    YYYY-MM-DD) so search results show their age. Exact duplicates of a stored fact are
    skipped. All or nothing: everything is embedded before anything is written. Returns how
    many were stored; raises if ChromaDB is unavailable."""
    if collection is None or embeddings is None:
        raise RuntimeError("ChromaDB is unavailable")
    existing = {_norm(d) for d in collection.get(include=["documents"])["documents"]}
    new = []
    for fact in facts:
        fact = (fact or "").strip()
        if fact and _norm(fact) not in existing:
            existing.add(_norm(fact))
            new.append(fact)
    if not new:
        return 0
    vectors = _embed_all(new)
    collection.add(ids=[uuid.uuid4().hex for _ in new], documents=new, embeddings=vectors,
                   metadatas=[{"added": added, "source": source} for _ in new])
    for fact in new:
        print(f"✓ Added to semantic memory: {fact}")
    return len(new)


def recent_facts(days: int = 7, today=None) -> list:
    """Owner facts learned in the last `days` days, oldest first."""
    if collection is None:
        return []
    cutoff = ((today or datetime.date.today()) - datetime.timedelta(days=days)).isoformat()
    got = collection.get(include=["documents", "metadatas"])
    rows = [((meta or {}).get("added", ""), doc)
            for doc, meta in zip(got["documents"], got["metadatas"])]
    return [doc for added, doc in sorted(rows) if added >= cutoff]


def _search_memory(query: str, n_results: int = 5) -> str:
    if is_guest():
        # Query ONLY this guest's collection — never the owner's scratchpad/collection.
        col = _guest_collection()
        if col is None or embeddings is None:
            return "No relevant memories found."
        try:
            res = col.query(query_embeddings=[embeddings.embed_query(query)], n_results=n_results)
            docs = res['documents'][0] if res.get('documents') else []
        except Exception as e:
            return f"[memory error: {str(e)}]"
        if not docs:
            return "No relevant memories found."
        return "What I remember:\n" + "\n".join(f"- {d}" for d in docs)

    output = []

    # 1. Working Memory (Scratchpad)
    if os.path.exists(SCRATCHPAD_PATH):
        with open(SCRATCHPAD_PATH, "r") as f:
            scratch_lines = [line.strip() for line in f.readlines() if line.strip()]
            if scratch_lines:
                output.append("Recent Working Memory (Today):")
                for line in scratch_lines:
                    output.append(f"- {line}")
                output.append("") # spacer
                
    # 2. Short-Term/Pointer Memory (ChromaDB)
    if collection is not None:
        try:
            vector = embeddings.embed_query(query)
            results = collection.query(
                query_embeddings=[vector],
                n_results=n_results
            )
            
            if results['documents'] and results['documents'][0]:
                output.append("Semantic Memory (Past):")
                metas = (results.get('metadatas') or [[]])[0] or []
                for i, doc in enumerate(results['documents'][0]):
                    added = (metas[i] or {}).get("added") if i < len(metas) else None
                    output.append(f"- {doc}" + (f" (noted {added})" if added else ""))
        except Exception as e:
            output.append(f"[ChromaDB Error: {str(e)}]")
            
    if not output:
        return "No relevant memories found."

    return "\n".join(output)


@tool
def search_memory(query: str, n_results: int = 5, config: RunnableConfig = None) -> str:
    """Use this tool to search the user's semantic memory for relevant facts or past events.
    Provide a search query (e.g., 'Does Satvik like coffee?', 'travel plans').
    Memories show when they were noted; when two conflict, trust the newer one.
    """
    tok, refusal = _tenant_scope(config)
    if refusal:
        return refusal
    try:
        return _search_memory(query, n_results)
    finally:
        if tok:
            reset_current_user(tok)

