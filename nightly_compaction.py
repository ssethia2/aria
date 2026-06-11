"""Tier 1 -> Tier 2 memory compaction (nightly cron; also run by run.sh).

LLM-extracts semantic facts from daily_scratchpad.txt, embeds them into ChromaDB,
wipes the scratchpad, then checks the Tier 3 consolidation threshold. See docs/adr/0002.
"""
import os
import uuid
from memory import SCRATCHPAD_PATH, collection, embeddings
from llm_router import get_llm
from dynamic_consolidation import consolidate_long_term_memory
from langchain_core.messages import HumanMessage, SystemMessage

def compact_scratchpad():
    if not os.path.exists(SCRATCHPAD_PATH):
        print("No scratchpad found. Nothing to compact.")
        return

    with open(SCRATCHPAD_PATH, "r") as f:
        raw_text = f.read().strip()

    if not raw_text:
        print("Scratchpad is empty. Nothing to compact.")
        return

    print("Compacting raw scratchpad using LLM...")
    try:
        llm = get_llm(temperature=0)
    except Exception as e:
        print(f"Failed to load LLM: {e}")
        return
    
    prompt = f"""
You are an AI assistant's memory manager. Read the following raw notes from today's
scratchpad and decide what deserves to enter the user's LONG-TERM memory.

PERSIST — anything with LASTING value, whether or not it is directly about the user:
facts about the user (preferences, plans, health, work, possessions), facts about
people in their life (names, relationships, birthdays, situations), and important
context about their world (home, projects, accounts, decisions made, ongoing
situations, useful reference details). The test: would a thoughtful human assistant
jot this in their notebook because it could matter weeks from now?

DISCARD — the day's activity log: lines tagged "[Aria proactive action ...]",
reminders that fired, routine email triage, Netflix household updates, news briefings
sent, and other one-off status events. These describe what happened today, not what
is worth knowing later.
Exception: persist an operational detail ONLY if it reveals something lasting
(e.g. the same automation failing repeatedly, or a new recurring obligation).

Format your output as a simple newline-separated list of clear, standalone sentences.
Do not include any introductory or concluding text, bullet points, numbers, or dashes.
If nothing qualifies for long-term memory, output exactly: NOTHING

Raw Notes:
{raw_text}
    """
    
    try:
        response = llm.invoke([SystemMessage(content="You extract clear semantic facts from raw notes."), HumanMessage(content=prompt)])
        content = response.content
        if isinstance(content, list):
            content = next((block['text'] for block in content if isinstance(block, dict) and block.get('type') == 'text'), str(content))
            
        if content.strip().upper() == "NOTHING":
            facts = []
        else:
            facts = [line.strip("- *0123456789. ") for line in content.split('\n') if line.strip()]

        raw_lines = len([l for l in raw_text.split('\n') if l.strip()])
        print(f"Compaction filter: {len(facts)} durable fact(s) kept from {raw_lines} scratchpad line(s).")

        if collection is None:
            print("ChromaDB collection is uninitialized. Cannot compact.")
            return

        for fact in facts:
            if not fact:
                continue
            vector = embeddings.embed_query(fact)
            doc_id = str(uuid.uuid4())
            collection.add(
                embeddings=[vector],
                documents=[fact],
                ids=[doc_id]
            )
            print(f"✓ Added to Short-Term Memory: {fact}")

        # Clear scratchpad
        with open(SCRATCHPAD_PATH, "w") as f:
            f.write("")
            
        print("Nightly compaction complete. Scratchpad wiped.")
        
        # Trigger Tier-3 Dynamic Consolidation Check
        consolidate_long_term_memory(threshold=100)
        
    except Exception as e:
        print(f"Failed during compaction: {e}")

if __name__ == "__main__":
    compact_scratchpad()
