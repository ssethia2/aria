"""Grocery list skill + recipe→ingredients.

A running grocery list Aria maintains (grocery_list.json, gitignored, host-
portable). The recipe→list flow is LLM-native: Aria figures out a dish's
ingredients (from given text, a recipe URL via fetch_webpage, or her own
knowledge) and drops them in via add_to_grocery_list — no hardcoded recipe
parser needed.

This is a LIST, not a store-cart integration: it captures what to buy. Pushing
it into an actual Amazon/Whole Foods cart is a separate (fragile) browser job.
"""
import json
import os
import re
from datetime import datetime

from langchain_core.tools import tool

LIST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'grocery_list.json')


def _load() -> list:
    try:
        with open(LIST_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list):
    with open(LIST_PATH, 'w') as f:
        json.dump(items, f, indent=2)


def _split_items(raw: str) -> list:
    """Split a blob of items on newlines, commas, and semicolons."""
    parts = re.split(r'[\n,;]+', raw)
    return [p.strip(' -*\t') for p in parts if p.strip(' -*\t')]


def add_items(item_strings) -> tuple:
    """Add items (str blob or list). Dedups case-insensitively.
    Returns (added, already_present)."""
    if isinstance(item_strings, str):
        new = _split_items(item_strings)
    else:
        new = [s.strip() for s in item_strings if s and s.strip()]

    items = _load()
    existing_lower = {i['item'].lower() for i in items}
    added, dup = [], []
    for n in new:
        if n.lower() in existing_lower:
            dup.append(n)
        else:
            items.append({'item': n, 'added': datetime.now().strftime('%Y-%m-%d')})
            existing_lower.add(n.lower())
            added.append(n)
    _save(items)
    return added, dup


@tool
def add_to_grocery_list(items: str) -> str:
    """Add one or more items to the grocery list. Separate multiple items with
    newlines or commas (e.g. "milk, eggs, 2 lbs chicken thighs").

    For a recipe or dish, work out the ingredients first (from the recipe text, a
    URL via fetch_webpage, or your own cooking knowledge), then pass them all here
    in one call — that's how "add everything for chicken tikka masala" works."""
    added, dup = add_items(items)
    if not added and not dup:
        return "Nothing to add."
    parts = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if dup:
        parts.append(f"Already on the list: {', '.join(dup)}")
    return ". ".join(parts) + f". ({len(_load())} items total.)"


@tool
def view_grocery_list() -> str:
    """Show the current grocery list. Use when asked what's on it / what to buy."""
    items = _load()
    if not items:
        return "The grocery list is empty."
    return "🛒 Grocery list:\n" + "\n".join(f"- {i['item']}" for i in items)


@tool
def remove_from_grocery_list(item: str) -> str:
    """Remove an item from the grocery list (matches on a substring, so "chicken"
    removes "2 lbs chicken thighs"). Use when the user already has something or
    changes their mind."""
    needle = item.strip().lower()
    items = _load()
    kept = [i for i in items if needle not in i['item'].lower()]
    removed = len(items) - len(kept)
    if not removed:
        return f"Nothing matching '{item}' on the list."
    _save(kept)
    return f"Removed {removed} item(s) matching '{item}'. ({len(kept)} left.)"


@tool
def clear_grocery_list() -> str:
    """Empty the grocery list — use after a shopping trip is done."""
    n = len(_load())
    _save([])
    return f"Cleared the grocery list ({n} items removed)."
