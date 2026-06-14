"""Tier 2 -> Tier 3 cold-storage consolidation (threshold-triggered).

When ChromaDB exceeds the vector threshold, LLM-summarizes granular vectors into a
long-form narrative in cold_storage/, deletes the originals, and leaves a single
pointer vector behind. Invoked from nightly_compaction.py. See docs/adr/0002.
"""
import os
import uuid
import datetime
from core.memory import DB_PATH, collection, embeddings
from core.llm_router import get_llm
from langchain_core.messages import HumanMessage, SystemMessage

COLD_STORAGE_DIR = os.path.join(os.path.dirname(__file__), "cold_storage")

def consolidate_long_term_memory(threshold=100):
    if collection is None:
        print("ChromaDB uninitialized.")
        return

    count = collection.count()
    print(f"Current Short-Term Memory Vector Count: {count}")
    
    if count < threshold:
        print(f"Threshold not met ({count}/{threshold}). Skipping Tier-3 consolidation.")
        return

    print("Triggering Long-Term Memory Cold Storage Consolidation...")
    
    # Fetch all records
    results = collection.get()
    documents = results.get("documents", [])
    ids = results.get("ids", [])
    
    if not documents:
        return

    raw_facts = "\n".join([f"- {doc}" for doc in documents])
    
    try:
        llm = get_llm(temperature=0)
    except Exception as e:
        print(f"Failed to load LLM for consolidation: {e}")
        return
        
    prompt = f"""
You are Aria's long-term memory archivist.
Read the following granular facts from Aria's short-term memory.
Summarize all these facts into a single, highly detailed, well-organized profile narrative about the user.
Group related facts together (e.g., Travel, Preferences, Hobbies, Work).
Make it read like a comprehensive biography or user manual.

Granular Facts:
{raw_facts}
    """
    
    try:
        response = llm.invoke([SystemMessage(content="You consolidate fragmented facts into comprehensive narratives."), HumanMessage(content=prompt)])
        content = response.content
        if isinstance(content, list):
            content = next((block['text'] for block in content if isinstance(block, dict) and block.get('type') == 'text'), str(content))
            
        long_form_narrative = content.strip()
        
        # Write to physically cold storage file
        if not os.path.exists(COLD_STORAGE_DIR):
            os.makedirs(COLD_STORAGE_DIR)
            
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"memory_archive_{timestamp}.txt"
        file_path = os.path.join(COLD_STORAGE_DIR, filename)
        
        with open(file_path, "w") as f:
            f.write(long_form_narrative)
            
        print(f"Wrote long-form narrative to {file_path}")
        
        # Create pointer vector
        pointer_fact = f"Extensive long-term memory archive created on {timestamp}. Contains deeply consolidated facts. To access this information, use the read_cold_storage tool with the filename: {filename}"
        pointer_vector = embeddings.embed_query(pointer_fact)
        pointer_id = str(uuid.uuid4())
        
        # Delete old granular vectors
        collection.delete(ids=ids)
        print(f"Deleted {len(ids)} discrete granular vectors from ChromaDB.")
        
        # Add pointer vector
        collection.add(
            embeddings=[pointer_vector],
            documents=[pointer_fact],
            ids=[pointer_id]
        )
        print("✓ Successfully dropped pointer vector into Short-Term Memory.")
        
    except Exception as e:
        print(f"Failed during consolidation: {e}")

if __name__ == "__main__":
    consolidate_long_term_memory()
