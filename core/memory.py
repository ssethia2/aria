"""Memory layer + LangChain memory tools (see docs/adr/0002 for the 3-tier design).

Tier 0: static profile.json. Tier 1: daily_scratchpad.txt (add_memory).
Tier 2: ChromaDB semantic store (search_memory). Tier 3: cold_storage/ narratives
(read_cold_storage). The nightly/threshold jobs that move data between tiers live in
nightly_compaction.py and dynamic_consolidation.py.
"""
import json
import os
import uuid
import chromadb
from langchain_core.tools import tool
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

PROFILE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profile.json")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chroma_db")
SCRATCHPAD_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "daily_scratchpad.txt")

# --- Layer 1: Core Identity (Static Profile) ---

def load_profile():
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

@tool
def add_memory(fact: str) -> str:
    """Use this tool to add a new memory, fact, preference, or event about the user.
    Provide a clear, detailed sentence (e.g., 'Satvik hates newsletters', 'Satvik is traveling to NY in July').
    """
    try:
        with open(SCRATCHPAD_PATH, "a") as f:
            f.write(fact + "\n")
        print(f"📝 Working Memory Added: {fact}")
        return f"Successfully remembered in working memory: {fact}"
    except Exception as e:
        return f"Failed to add memory: {str(e)}"

@tool
def search_memory(query: str, n_results: int = 3) -> str:
    """Use this tool to search the user's semantic memory for relevant facts or past events.
    Provide a search query (e.g., 'Does Satvik like coffee?', 'travel plans').
    """
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
                for doc in results['documents'][0]:
                    output.append(f"- {doc}")
        except Exception as e:
            output.append(f"[ChromaDB Error: {str(e)}]")
            
    if not output:
        return "No relevant memories found."
        
    return "\n".join(output)

@tool
def read_cold_storage(filename: str) -> str:
    """Use this tool to read a long-term memory archive file from cold storage when directed to by a pointer vector.
    Provide the exact filename (e.g., 'memory_archive_20260317_120000.txt').
    """
    cold_storage_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cold_storage")
    filepath = os.path.join(cold_storage_dir, filename)
    
    if not os.path.exists(filepath):
        return f"Error: Cold storage file '{filename}' not found."
        
    try:
        with open(filepath, "r") as f:
            return f"--- DEEP LONG-TERM MEMORY ARCHIVE ---\n{f.read()}\n-------------------------------------"
    except Exception as e:
        return f"Failed to read cold storage: {str(e)}"

