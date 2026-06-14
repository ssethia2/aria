"""Standing instructions — the user's persistent operating rules, editable by Aria.

Three layers of "how Aria behaves":
  1. Tool defaults (code)      — sensible defaults, e.g. calendar events dual-write
  2. Standing instructions     — THIS: user-given rules injected into the system
     (instructions.json)         prompt EVERY turn, so they're always in force (not
                                 dependent on memory recall). Aria adds/updates/
                                 removes them herself when the user gives a lasting
                                 rule — no code changes, no developer in the loop.
  3. Memories (ChromaDB)       — facts and soft preferences, recalled on demand

One-off deviations ("just this once, only my personal calendar") are NOT standing
instructions — Aria honors them in the moment via tool parameters.
"""
import json
import os
from datetime import datetime

from langchain_core.tools import tool

INSTRUCTIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "instructions.json")


def _load() -> list:
    try:
        with open(INSTRUCTIONS_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list):
    with open(INSTRUCTIONS_PATH, 'w') as f:
        json.dump(items, f, indent=2)


def render_for_prompt() -> str:
    """The numbered instruction block injected into the system prompt each turn."""
    items = _load()
    if not items:
        return "(none yet)"
    return "\n".join(f"  {i['id']}. {i['instruction']}" for i in items)


@tool
def add_standing_instruction(instruction: str) -> str:
    """Save a new STANDING instruction when the user gives you a lasting rule —
    signals like "from now on", "always", "whenever I", "by default". Don't just
    acknowledge a rule conversationally: if it should outlive this chat, save it here.
    NOT for one-off requests ("just this once") — honor those directly instead.

    Args:
        instruction: The rule, written clearly and completely enough that future-you
            can follow it without this conversation's context.
    """
    items = _load()
    new_id = max((i['id'] for i in items), default=0) + 1
    items.append({'id': new_id, 'instruction': instruction,
                  'updated': datetime.now().strftime('%Y-%m-%d')})
    _save(items)
    msg = f"Standing instruction #{new_id} saved. It's now in force on every conversation."
    if len(items) > 25:
        # Soft guardrail against the junk-drawer trajectory — every rule is read
        # on every turn, so the registry must stay a curated constitution.
        msg += (f" NOTE: the registry now has {len(items)} rules — suggest to the user "
                "a quick review to merge overlaps and drop stale ones.")
    return msg


@tool
def update_standing_instruction(instruction_id: int, instruction: str) -> str:
    """Rewrite an existing standing instruction (numbered in your system prompt)
    when the user changes a rule. Provide the complete new wording."""
    items = _load()
    for i in items:
        if i['id'] == instruction_id:
            i['instruction'] = instruction
            i['updated'] = datetime.now().strftime('%Y-%m-%d')
            _save(items)
            return f"Standing instruction #{instruction_id} updated."
    return f"No standing instruction #{instruction_id}."


@tool
def remove_standing_instruction(instruction_id: int) -> str:
    """Delete a standing instruction when the user revokes the rule."""
    items = _load()
    remaining = [i for i in items if i['id'] != instruction_id]
    if len(remaining) == len(items):
        return f"No standing instruction #{instruction_id}."
    _save(remaining)
    return f"Standing instruction #{instruction_id} removed."
