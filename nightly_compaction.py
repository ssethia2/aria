"""Tier 1 -> Tier 2 memory compaction (nightly cron; also run by run.sh).

LLM-extracts semantic facts from daily_scratchpad.txt and the day's conversation log,
embeds them into ChromaDB, then wipes both. See docs/adr/0002 and 0008.
"""
import datetime
import os
from memory import SCRATCHPAD_PATH, CONVERSATION_PATH, fact_lines, load_exchanges, store_facts
from llm_router import get_llm
from langchain_core.messages import HumanMessage, SystemMessage

CONVERSATION_CHARS = 60000      # newest exchanges first if a day runs long


def _conversation_text() -> str:
    lines, total = [], 0
    for ex in reversed(load_exchanges(CONVERSATION_PATH)):
        block = f"[{ex.get('at', '?')}] User: {ex.get('user', '')}\nAria: {ex.get('aria', '')}"
        if total + len(block) > CONVERSATION_CHARS:
            break
        lines.append(block)
        total += len(block)
    return "\n\n".join(reversed(lines))


def _wipe(path):
    if os.path.exists(path):
        with open(path, "w") as f:
            f.write("")


def compact_scratchpad():
    raw_text = ""
    if os.path.exists(SCRATCHPAD_PATH):
        with open(SCRATCHPAD_PATH, "r") as f:
            raw_text = f.read().strip()
    conversation = _conversation_text()

    if not raw_text and not conversation:
        print("Scratchpad and conversation log are empty. Nothing to compact.")
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

The conversations are his exchanges with you since the last compaction. From them,
PERSIST what he told you or what you settled together that could matter later: plans,
bookings and itineraries, decisions, preferences, corrections to what you had wrong.
Keep the specifics in the fact itself (dates, times, flight numbers, confirmation codes,
places, who is involved), one fact per booking or leg, so a later search finds it whole.
DISCARD the back-and-forth itself, your offers and questions, and anything that only
mattered in the moment. Never store a password, PIN, card number or one-time code, even
when one appears.

Format your output as a simple newline-separated list of clear, standalone sentences.
Do not include any introductory or concluding text, bullet points, numbers, or dashes.
If nothing qualifies for long-term memory, output exactly: NOTHING

Raw Notes:
{raw_text or "(none)"}

Conversations:
{conversation or "(none)"}
    """
    
    try:
        response = llm.invoke([SystemMessage(content="You extract clear semantic facts from raw notes."), HumanMessage(content=prompt)])
        content = response.content
        if isinstance(content, list):
            content = next((block['text'] for block in content if isinstance(block, dict) and block.get('type') == 'text'), str(content))
            
        if content.strip().upper() == "NOTHING":
            facts = []
        else:
            facts = fact_lines(content)

        raw_lines = len([l for l in raw_text.split('\n') if l.strip()])
        print(f"Compaction filter: {len(facts)} durable fact(s) kept from {raw_lines} "
              f"scratchpad line(s) and {conversation.count('] User: ')} exchange(s).")

        # Date facts by the last write — when they were told, not when the morning job
        # happens to run.
        written = [os.path.getmtime(p) for p in (SCRATCHPAD_PATH, CONVERSATION_PATH)
                   if os.path.exists(p)]
        noted = datetime.date.fromtimestamp(max(written)).isoformat()
        stored = store_facts(facts, added=noted)   # raises if ChromaDB is down -> keep both
        print(f"{stored} new fact(s) stored in semantic memory.")

        _wipe(SCRATCHPAD_PATH)
        _wipe(CONVERSATION_PATH)
        print("Nightly compaction complete. Scratchpad and conversation log wiped.")

    except Exception as e:
        print(f"Failed during compaction: {e}")

if __name__ == "__main__":
    compact_scratchpad()
