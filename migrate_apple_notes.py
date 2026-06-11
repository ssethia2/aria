"""One-time Apple Notes -> Aria migration (run on the Mac, 2026-06).

Exports every unlocked note via JXA (osascript) and writes them as markdown
into notes/imported/ — Aria's plain-file notes store, which travels to any
future host. Password-protected notes are skipped by flag (their bodies are
never read). Re-running creates duplicates; it's a one-shot.

macOS will prompt "Terminal wants access to control Notes" on first run —
approve it.
"""
import json
import os
import subprocess
import sys
import tempfile

from bs4 import BeautifulSoup

from skills.notes_manager import write_note_file

JXA = r'''
const app = Application('Notes');
// Bulk property fetches: ONE AppleEvent per property (vs. four per note),
// which is what keeps a large library under the 120s AppleEvent timeout.
const names = app.notes.name();
const locked = app.notes.passwordProtected();
let folders = [];
try { folders = app.notes.container.name(); } catch (e) {}
let modified = [];
try { modified = app.notes.modificationDate(); } catch (e) {}

let bodies = null;
try {
    bodies = app.notes.body();           // fast path: one event for all bodies
} catch (e) {
    bodies = null;                       // fall back to per-note below
}

const out = [];
for (let i = 0; i < names.length; i++) {
    if (locked[i]) { out.push({locked: true}); continue; }
    let body = '';
    if (bodies !== null) {
        body = bodies[i] || '';
    } else {
        try { body = app.notes[i].body() || ''; } catch (e) { out.push({locked: true}); continue; }
    }
    out.push({
        name: names[i],
        body: body,
        folder: folders[i] || '',
        modified: modified[i] ? modified[i].toISOString() : '',
        locked: false
    });
}
JSON.stringify(out);
'''


def export_from_notes_app() -> list:
    print("Reading Apple Notes (approve the automation prompt if macOS asks)...")
    result = subprocess.run(['osascript', '-l', 'JavaScript', '-e', JXA],
                            capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f"osascript failed: {result.stderr.strip()}")
        print("If this is a permissions error: System Settings -> Privacy & Security "
              "-> Automation -> allow your terminal to control Notes, then rerun.")
        sys.exit(1)
    return json.loads(result.stdout)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or '', 'html.parser')
    for br in soup.find_all('br'):
        br.replace_with('\n')
    lines = []
    for block in soup.find_all(['h1', 'h2', 'h3', 'div', 'p', 'li']):
        text = block.get_text().strip()
        if text:
            prefix = '- ' if block.name == 'li' else ''
            lines.append(prefix + text)
    text = '\n'.join(lines) if lines else soup.get_text()
    return text.strip()


def main():
    notes = export_from_notes_app()
    migrated, skipped_locked, skipped_empty = 0, 0, 0
    for n in notes:
        if n.get('locked'):
            skipped_locked += 1
            continue
        title = (n.get('name') or 'Untitled').strip()
        body = html_to_text(n.get('body', ''))
        # Apple repeats the title as the body's first line — drop the echo.
        if body.startswith(title):
            body = body[len(title):].strip()
        if not body:
            skipped_empty += 1
            continue
        header = f"(Imported from Apple Notes, folder: {n.get('folder') or '—'}, " \
                 f"last modified {n.get('modified', '?')[:10]})"
        write_note_file(title, f"{header}\n\n{body}", subdir='imported')
        migrated += 1

    print(f"\n✅ Migrated {migrated} notes into notes/imported/")
    print(f"   Skipped: {skipped_locked} locked (left in Apple Notes), {skipped_empty} empty")
    print("Ask Aria to 'search my notes' to use them — or to go through them and "
          "extract any todos into commitments.")


if __name__ == '__main__':
    main()
