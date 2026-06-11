"""Notes skill — Aria IS the user's notes system (2026-06 onward).

Plain markdown files in notes/ (gitignored, rsyncs to any future host — no
platform lock-in, unlike the Apple Notes they replaced; that archive was
imported via migrate_apple_notes.py into notes/imported/). Todos do NOT live
here — actionable items belong in the commitment store.

Search is keyword-based (all terms must appear); semantic search over notes is
a future upgrade if the corpus outgrows grep.
"""
import os
import re
from datetime import datetime

from langchain_core.tools import tool

NOTES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'notes')


def _slugify(title: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    return slug or 'untitled'


def _path_for(title: str, subdir: str = '') -> str:
    d = os.path.join(NOTES_DIR, subdir) if subdir else NOTES_DIR
    os.makedirs(d, exist_ok=True)
    base = _slugify(title)
    path = os.path.join(d, f"{base}.md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(d, f"{base}-{n}.md")
        n += 1
    return path


def _iter_notes():
    if not os.path.isdir(NOTES_DIR):
        return
    for root, _dirs, files in os.walk(NOTES_DIR):
        for f in sorted(files):
            if f.endswith('.md'):
                yield os.path.join(root, f)


def write_note_file(title: str, content: str, subdir: str = '') -> str:
    """Create a note file (used by tools and the Apple Notes migrator)."""
    path = _path_for(title, subdir)
    stamp = datetime.now().strftime('%Y-%m-%d')
    with open(path, 'w') as f:
        f.write(f"# {title}\n\n{content.strip()}\n")
    return path


@tool
def create_note(title: str, content: str) -> str:
    """Save a note for the user — reference info, lists, ideas, plans. You are his
    notes system now. NOT for actionable todos (those are commitments) or facts
    about people (those go in their person record)."""
    path = write_note_file(title, content)
    return f"Note saved: '{title}' ({os.path.relpath(path, NOTES_DIR)})."


@tool
def append_to_note(title_query: str, content: str) -> str:
    """Add to an existing note (e.g. another item for a list). Finds the note by
    title; creates it if nothing matches."""
    matches = [p for p in _iter_notes()
               if title_query.lower() in os.path.basename(p).replace('-', ' ')]
    if not matches:
        write_note_file(title_query, content)
        return f"No existing note matched — created '{title_query}' instead."
    with open(matches[0], 'a') as f:
        f.write(f"\n{content.strip()}\n")
    return f"Appended to {os.path.relpath(matches[0], NOTES_DIR)}."


@tool
def search_notes(query: str) -> str:
    """Search the user's notes (including the imported Apple Notes archive).
    Use when he asks about something he noted down, a list, plan, or reference."""
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return "Give me something to search for."
    hits = []
    for path in _iter_notes():
        try:
            with open(path) as f:
                text = f.read()
        except Exception:
            continue
        lower = text.lower()
        if all(t in lower for t in terms):
            idx = lower.find(terms[0])
            snippet = ' '.join(text[max(0, idx - 60):idx + 120].split())
            hits.append(f"- {os.path.relpath(path, NOTES_DIR)}: …{snippet}…")
        if len(hits) >= 8:
            break
    return ("Matching notes:\n" + "\n".join(hits)) if hits else f"No notes match {query!r}."


@tool
def read_note(filename: str) -> str:
    """Read a full note by its filename (as returned by search_notes,
    e.g. 'imported/travel-ideas.md')."""
    path = os.path.join(NOTES_DIR, filename)
    if not os.path.isfile(path) or not os.path.realpath(path).startswith(os.path.realpath(NOTES_DIR)):
        return f"No note at {filename!r}."
    with open(path) as f:
        text = f.read()
    return text[:6000] + (" ...[truncated]" if len(text) > 6000 else "")
