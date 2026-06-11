"""Commitment skill — Aria's core loop: capture, track, chase, and close the things
the user commits to. Built from the 2026-06 needs interview: what actually slips are
replies owed, deadlines/renewals, people dates, and verbal promises that never enter
any system. Telling Aria IS the system.

Kinds: reply_owed | deadline | people_date | promise. people_date can recur yearly
(completing it rolls it to next year instead of closing). A "reminder" is just a
commitment with a due_time — the engine pings those at their moment.

Agent tools: add_commitment, list_commitments, complete_commitment, drop_commitment.
Engine/briefing helpers: get_due_commitments, get_timed_due_now, get_upcoming_commitments.
Storage: `commitments` table in aria_calendar.db; open rows from the legacy reminders
table are migrated in on first init.
"""
import os
import sqlite3
from datetime import datetime, timedelta

from langchain_core.tools import tool

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'aria_calendar.db')

VALID_KINDS = {'reply_owed', 'deadline', 'people_date', 'promise'}


def _conn():
    init_db()
    return sqlite3.connect(DB_PATH)


def init_db():
    """Create the commitments table; migrate open legacy reminders on first run."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'promise',
            who TEXT,
            due_date TEXT,
            due_time TEXT,
            recurring TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            source TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    ''')
    # One-time migration: legacy reminders (calendar_manager) become promises.
    cursor.execute("SELECT COUNT(*) FROM commitments")
    if cursor.fetchone()[0] == 0:
        try:
            cursor.execute("SELECT task, target_date, created_at FROM reminders WHERE completed = 0")
            for task, target_date, created_at in cursor.fetchall():
                cursor.execute('''
                    INSERT INTO commitments (description, kind, due_date, status, source, created_at)
                    VALUES (?, 'promise', ?, 'open', 'migrated', ?)
                ''', (task, target_date, created_at))
        except sqlite3.OperationalError:
            pass  # no legacy table — fresh install
    conn.commit()
    conn.close()


def _rows_to_dicts(rows):
    keys = ['id', 'description', 'kind', 'who', 'due_date', 'due_time', 'recurring',
            'status', 'source', 'created_at']
    return [dict(zip(keys, r)) for r in rows]


_SELECT = ("SELECT id, description, kind, who, due_date, due_time, recurring, status, "
           "source, created_at FROM commitments")


def add(description, kind='promise', who=None, due_date=None, due_time=None,
        recurring=None, source='chat'):
    """Insert a commitment; returns its id."""
    if kind not in VALID_KINDS:
        kind = 'promise'
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO commitments (description, kind, who, due_date, due_time, recurring,
                                 status, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
    ''', (description, kind, who, due_date, due_time, recurring, source,
          datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    cid = cursor.lastrowid
    conn.close()
    return cid


def get_open_commitments():
    """All open commitments, dated ones first (soonest due), undated last."""
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' "
        "ORDER BY due_date IS NULL, due_date ASC, id ASC").fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def get_due_commitments(today=None):
    """Open commitments due today or overdue (date-granular)."""
    today = today or datetime.now().strftime('%Y-%m-%d')
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' AND due_date IS NOT NULL AND due_date <= ? "
        "ORDER BY due_date ASC", (today,)).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def get_upcoming_commitments(days=7, today=None):
    """Open commitments due within the next `days` days (excluding today/overdue)."""
    today = today or datetime.now().strftime('%Y-%m-%d')
    horizon = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=days)).strftime('%Y-%m-%d')
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' AND due_date > ? AND due_date <= ? "
        "ORDER BY due_date ASC", (today, horizon)).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def get_timed_due_now(now=None):
    """Open commitments scheduled for a specific time today whose moment has arrived."""
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    hhmm = now.strftime('%H:%M')
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' AND due_date = ? AND due_time IS NOT NULL "
        "AND due_time <= ?", (today, hhmm)).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def complete(commitment_id):
    """Mark done. Yearly-recurring dates roll to next year instead of closing."""
    conn = _conn()
    row = conn.execute(
        "SELECT description, recurring, due_date FROM commitments WHERE id = ? AND status = 'open'",
        (commitment_id,)).fetchone()
    if not row:
        conn.close()
        return None
    description, recurring, due_date = row
    if recurring == 'yearly' and due_date:
        next_due = f"{int(due_date[:4]) + 1}{due_date[4:]}"
        conn.execute("UPDATE commitments SET due_date = ? WHERE id = ?",
                     (next_due, commitment_id))
        result = f"'{description}' done for this year — rolled to {next_due}."
    else:
        conn.execute(
            "UPDATE commitments SET status = 'done', completed_at = ? WHERE id = ?",
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), commitment_id))
        result = f"'{description}' marked done."
    conn.commit()
    conn.close()
    return result


def drop(commitment_id):
    conn = _conn()
    cursor = conn.execute(
        "UPDATE commitments SET status = 'dropped' WHERE id = ? AND status = 'open'",
        (commitment_id,))
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    return changed > 0


def format_line(c, today=None):
    """One human-readable line for briefings/digests/chat lists."""
    today = today or datetime.now().strftime('%Y-%m-%d')
    parts = [f"#{c['id']} {c['description']}"]
    if c['who'] and c['who'] not in c['description']:
        parts.append(f"({c['who']})")
    if c['due_date']:
        when = c['due_date'] + (f" {c['due_time']}" if c['due_time'] else "")
        marker = " ⚠️ OVERDUE" if c['due_date'] < today else ""
        parts.append(f"— due {when}{marker}")
    return " ".join(parts)


# --- Agent tools ---

@tool
def add_commitment(description: str, kind: str = 'promise', who: str = None,
                   due_date_iso: str = None, due_time: str = None,
                   recurring_yearly: bool = False) -> str:
    """Track something the user has committed to or must not forget. BE PROACTIVE:
    when the user mentions a promise, a deadline, someone's birthday, or a reply they
    owe — even in passing — offer to track it, or just track it if they asked.

    Args:
        description: What needs to happen (e.g. "Reply to Rohan about the trip",
            "Renew passport", "Mom's birthday", "Help Dad move").
        kind: One of 'reply_owed', 'deadline', 'people_date', 'promise'.
        who: The person involved, if any (e.g. "Rohan", "Mom").
        due_date_iso: Due date YYYY-MM-DD. Compute exact dates from words like
            "tomorrow"/"Friday" using the current date. Omit if genuinely undated.
        due_time: HH:MM 24h — ONLY when the user names a specific time ("at 3pm" -> 15:00).
            Timed commitments get pinged at that moment.
        recurring_yearly: True for birthdays/anniversaries — completing rolls to next year.
    """
    if due_date_iso:
        try:
            datetime.strptime(due_date_iso, '%Y-%m-%d')
        except ValueError:
            return f"Error: due_date_iso must be YYYY-MM-DD, got {due_date_iso}."
    if due_time:
        try:
            datetime.strptime(due_time, '%H:%M')
        except ValueError:
            return f"Error: due_time must be HH:MM (24h), got {due_time}."
    cid = add(description, kind=kind, who=who, due_date=due_date_iso, due_time=due_time,
              recurring=('yearly' if recurring_yearly else None))
    when = f" for {due_date_iso}{' ' + due_time if due_time else ''}" if due_date_iso else ""
    return f"Tracked commitment #{cid}: '{description}'{when}."


@tool
def list_commitments(include_upcoming_only: bool = False) -> str:
    """Show the user's open commitments. Use when they ask what they owe, what's
    pending, what's due, or what's on their plate.

    Args:
        include_upcoming_only: True to show only items due in the next 7 days.
    """
    items = get_upcoming_commitments() + get_due_commitments() if include_upcoming_only \
        else get_open_commitments()
    if not items:
        return "No open commitments — all clear."
    seen, lines = set(), []
    for c in sorted(items, key=lambda x: (x['due_date'] is None, x['due_date'] or '', x['id'])):
        if c['id'] in seen:
            continue
        seen.add(c['id'])
        lines.append(format_line(c))
    return "Open commitments:\n" + "\n".join(lines)


@tool
def complete_commitment(commitment_id: int) -> str:
    """Mark a commitment as done when the user says they did it. Use the #id from
    list_commitments. Yearly dates (birthdays) roll to next year automatically."""
    result = complete(commitment_id)
    return result or f"No open commitment with id {commitment_id}."


@tool
def drop_commitment(commitment_id: int) -> str:
    """Drop a commitment the user no longer intends to do (without marking it done)."""
    return (f"Dropped commitment #{commitment_id}." if drop(commitment_id)
            else f"No open commitment with id {commitment_id}.")
