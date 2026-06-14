"""People model — first-class records for the humans in the user's life.

Born from the girlfriend incident (plan Phase 8): facts about people used to be
loose strings in semantic memory, so Aria couldn't resolve "my girlfriend" to a
person, know what she already knew, or connect a birthday to a commitment. Now
each person is a dossier: canonical name, relation, aliases, birthday, notes,
and the people-date plumbing to never miss their day.

A compact roster (name + relation) is injected into the system prompt each turn
so references resolve instantly; full dossiers load on demand via get_person.
This passes the state-vs-policy test: the roster is bounded by who is actually
in the user's life (intent), not by time, and it churns rarely.

Storage: people.json (gitignored).
"""
import json
import os
from datetime import datetime

from langchain_core.tools import tool

PEOPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "people.json")


def _load() -> list:
    try:
        with open(PEOPLE_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list):
    with open(PEOPLE_PATH, 'w') as f:
        json.dump(items, f, indent=2)


def _find(items, name: str):
    """Match by canonical name or alias, case-insensitively."""
    needle = name.strip().lower()
    for p in items:
        if p['name'].lower() == needle:
            return p
        if any(a.lower() == needle for a in p.get('aliases', [])):
            return p
    return None


def roster_for_prompt() -> str:
    """One compact line per person for the system prompt."""
    items = _load()
    if not items:
        return "(no one recorded yet)"
    parts = []
    for p in items:
        label = p['name']
        if p.get('relation'):
            label += f" ({p['relation']})"
        parts.append(label)
    return ", ".join(parts)


def _next_birthday_occurrence(birthday_iso: str, today: datetime = None) -> str:
    """The next upcoming MM-DD as a full date (this year if not yet passed)."""
    today = today or datetime.now()
    month_day = birthday_iso[5:]  # works for YYYY-MM-DD
    this_year = f"{today.year}-{month_day}"
    return this_year if this_year >= today.strftime('%Y-%m-%d') else f"{today.year + 1}-{month_day}"


@tool
def remember_person(name: str, relation: str = None, birthday_iso: str = None,
                    note: str = None, alias: str = None) -> str:
    """Create or update Aria's record of a person in the user's life. Use whenever
    you learn WHO someone is (their name, their relation to the user) or a lasting
    fact about them. This is how "my girlfriend" becomes a real person you know.

    Args:
        name: Canonical name (e.g. "Priya"). Updates the existing record if known.
        relation: Their relation to the user (e.g. "girlfriend", "dad", "friend",
            "coworker").
        birthday_iso: Their birthday as YYYY-MM-DD (use year 1900 if year unknown).
            Saving a birthday automatically creates a yearly people_date commitment.
        note: A lasting fact about them (e.g. "favorite flowers are tulips").
            Appends to their notes.
        alias: Another way the user refers to them (e.g. "my girlfriend", "gf").
    """
    items = _load()
    person = _find(items, name)
    created = person is None
    if created:
        person = {'name': name.strip(), 'relation': None, 'aliases': [],
                  'birthday': None, 'notes': [], 'updated': None}
        items.append(person)

    if relation:
        person['relation'] = relation
    if alias and alias.lower() not in [a.lower() for a in person['aliases']]:
        person['aliases'].append(alias)
    if note:
        person['notes'].append(note)
    changed_birthday = birthday_iso and person.get('birthday') != birthday_iso
    if birthday_iso:
        person['birthday'] = birthday_iso
    person['updated'] = datetime.now().strftime('%Y-%m-%d')
    _save(items)

    extras = []
    if changed_birthday:
        try:
            from skills import commitment_manager
            existing = [c for c in commitment_manager.get_open_commitments()
                        if c['kind'] == 'people_date' and (c.get('who') or '').lower() == person['name'].lower()]
            if not existing:
                due = _next_birthday_occurrence(birthday_iso)
                commitment_manager.add(description=f"{person['name']}'s birthday",
                                       kind='people_date', who=person['name'],
                                       due_date=due, recurring='yearly', source='people')
                extras.append(f"birthday tracked yearly (next: {due})")
        except Exception as e:
            print(f"[people] couldn't create birthday commitment: {e}")

    verb = "Now I know" if created else "Updated"
    msg = f"{verb} {person['name']}" + (f" ({person['relation']})" if person.get('relation') else "") + "."
    if extras:
        msg += " " + "; ".join(extras) + "."
    return msg


@tool
def get_person(name: str) -> str:
    """Everything Aria knows about a person — use before asking the user something
    you might already know, or when context about someone would help."""
    person = _find(_load(), name)
    if not person:
        return f"I don't have a record for '{name}' yet."
    lines = [f"{person['name']}" + (f" — {person['relation']}" if person.get('relation') else "")]
    if person.get('aliases'):
        lines.append(f"Also known as: {', '.join(person['aliases'])}")
    if person.get('birthday'):
        lines.append(f"Birthday: {person['birthday']}")
    for n in person.get('notes', []):
        lines.append(f"- {n}")
    return "\n".join(lines)


@tool
def list_people() -> str:
    """List everyone Aria knows in the user's life, with relations."""
    items = _load()
    if not items:
        return "I don't know anyone in the user's life yet."
    return "\n".join(
        f"- {p['name']}" + (f" ({p['relation']})" if p.get('relation') else "")
        + (f", birthday {p['birthday']}" if p.get('birthday') else "")
        for p in items)
