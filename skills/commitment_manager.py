"""Commitment skill — Aria's core loop: capture, track, chase, and close the things
the user commits to. Built from the 2026-06 needs interview: what actually slips are
replies owed, deadlines/renewals, people dates, and verbal promises that never enter
any system. Telling Aria IS the system.

Kinds: reply_owed | deadline | people_date | promise. people_date can recur yearly
(completing it rolls it to next year instead of closing). A "reminder" is just a
commitment with a due_time — the engine pings those at their moment.

Agent tools: add_commitment, list_commitments, complete_commitment, drop_commitment,
reschedule_commitment, snooze_commitment, reopen_commitment (+ analyze_commitments).
Every mutation lands in the append-only commitment_events audit trail (created/completed/
dropped/rescheduled/snoozed/nudged/reopened) — the substrate for slip patterns and
honest reopens after a mis-completion.
Engine/briefing helpers: get_due_commitments, get_timed_due_now, get_upcoming_commitments.
Storage: `commitments` table in aria_calendar.db; open rows from the legacy reminders
table are migrated in on first init.
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from tenant import get_current_user, scope_from_config, reset_current_user, OWNER_ID

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'aria_calendar.db')


def _scope():
    """(SQL predicate, params) restricting commitments to the current principal. Uniform:
    every row is keyed by its owner's user_id ("owner" for the owner, the guest id for a
    guest), so this is the same predicate for everyone — no privileged unscoped path."""
    return "tenant = ?", [get_current_user()]

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
            completed_at TEXT,
            tenant TEXT
        )
    ''')
    # Multi-user isolation: add the tenant column to pre-existing DBs (owner rows = NULL).
    cols = [r[1] for r in cursor.execute("PRAGMA table_info(commitments)").fetchall()]
    if 'tenant' not in cols:
        cursor.execute("ALTER TABLE commitments ADD COLUMN tenant TEXT")
    # v2: snooze — chase/nudges stay quiet about an item until this date (YYYY-MM-DD).
    if 'snoozed_until' not in cols:
        try:
            cursor.execute("ALTER TABLE commitments ADD COLUMN snoozed_until TEXT")
        except sqlite3.OperationalError:
            pass  # already added
    # v2: append-only event log — the audit trail behind slip patterns ("pushed 3x"),
    # honest reopens after a mis-completion, and nudge-history for the chase loop.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS commitment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            commitment_id INTEGER NOT NULL,
            event TEXT NOT NULL,      -- created|completed|dropped|rescheduled|snoozed|nudged|reopened
            detail TEXT,
            at TEXT NOT NULL
        )
    ''')
    # Uniform principals: the owner is now tenant "owner" (was the implicit NULL tenant).
    # Idempotent — only the legacy unkeyed owner rows match.
    cursor.execute("UPDATE commitments SET tenant = ? WHERE tenant IS NULL", (OWNER_ID,))
    # One-time migration: legacy reminders (calendar_manager) become promises.
    cursor.execute("SELECT COUNT(*) FROM commitments")
    if cursor.fetchone()[0] == 0:
        try:
            cursor.execute("SELECT task, target_date, created_at FROM reminders WHERE completed = 0")
            for task, target_date, created_at in cursor.fetchall():
                cursor.execute('''
                    INSERT INTO commitments (description, kind, due_date, status, source,
                                             created_at, tenant)
                    VALUES (?, 'promise', ?, 'open', 'migrated', ?, ?)
                ''', (task, target_date, created_at, OWNER_ID))
        except sqlite3.OperationalError:
            pass  # no legacy table — fresh install
    conn.commit()
    conn.close()


def _rows_to_dicts(rows):
    keys = ['id', 'description', 'kind', 'who', 'due_date', 'due_time', 'recurring',
            'status', 'source', 'created_at', 'snoozed_until']
    return [dict(zip(keys, r)) for r in rows]


_SELECT = ("SELECT id, description, kind, who, due_date, due_time, recurring, status, "
           "source, created_at, snoozed_until FROM commitments")


def add(description, kind='promise', who=None, due_date=None, due_time=None,
        recurring=None, source='chat'):
    """Insert a commitment; returns its id."""
    if kind not in VALID_KINDS:
        kind = 'promise'
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO commitments (description, kind, who, due_date, due_time, recurring,
                                 status, source, created_at, tenant)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
    ''', (description, kind, who, due_date, due_time, recurring, source,
          datetime.now().strftime('%Y-%m-%d %H:%M:%S'), get_current_user()))
    conn.commit()
    cid = cursor.lastrowid
    conn.close()
    log_event(cid, 'created', f"{kind}, due {due_date or 'undated'}")
    return cid


def log_event(commitment_id, event, detail=None):
    """Append to the audit trail. Never raises — logging must not break the operation."""
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO commitment_events (commitment_id, event, detail, at) VALUES (?,?,?,?)",
            (commitment_id, event, detail,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[commitments] event log failed: {e}")


def events_for(commitment_id):
    """This commitment's audit trail, oldest first: [{event, detail, at}]."""
    conn = _conn()
    rows = conn.execute(
        "SELECT event, detail, at FROM commitment_events WHERE commitment_id = ? ORDER BY id",
        [commitment_id]).fetchall()
    conn.close()
    return [dict(zip(('event', 'detail', 'at'), r)) for r in rows]


def slip_counts(commitment_ids):
    """{commitment_id: reschedule_count} for the given ids — the 'pushed 3x' signal the
    chase loop and pattern miner feed on."""
    if not commitment_ids:
        return {}
    qs = ",".join("?" * len(commitment_ids))
    conn = _conn()
    rows = conn.execute(
        f"SELECT commitment_id, COUNT(*) FROM commitment_events "
        f"WHERE event = 'rescheduled' AND commitment_id IN ({qs}) GROUP BY commitment_id",
        list(commitment_ids)).fetchall()
    conn.close()
    return dict(rows)


def reschedule(commitment_id, new_due_date, new_due_time=None):
    """Move a commitment's due date/time (history preserved via the event log).
    Returns the description, or None if no such open commitment."""
    pred, sp = _scope()
    conn = _conn()
    row = conn.execute(
        f"SELECT description, due_date, due_time FROM commitments WHERE id = ? "
        f"AND status = 'open' AND {pred}", [commitment_id, *sp]).fetchone()
    if not row:
        conn.close()
        return None
    description, old_date, old_time = row
    conn.execute(
        "UPDATE commitments SET due_date = ?, due_time = ?, snoozed_until = NULL WHERE id = ?",
        (new_due_date, new_due_time, commitment_id))
    conn.commit()
    conn.close()
    log_event(commitment_id, 'rescheduled',
              f"{old_date or 'undated'}{' ' + old_time if old_time else ''} -> "
              f"{new_due_date}{' ' + new_due_time if new_due_time else ''}")
    return description


def snooze(commitment_id, until_date):
    """Silence chase/nudges for this commitment until `until_date` (stays open; still
    visible in lists). Returns the description, or None if no such open commitment."""
    pred, sp = _scope()
    conn = _conn()
    row = conn.execute(
        f"SELECT description FROM commitments WHERE id = ? AND status = 'open' AND {pred}",
        [commitment_id, *sp]).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("UPDATE commitments SET snoozed_until = ? WHERE id = ?",
                 (until_date, commitment_id))
    conn.commit()
    conn.close()
    log_event(commitment_id, 'snoozed', f"until {until_date}")
    return row[0]


def reopen(commitment_id):
    """Reopen a done/dropped commitment — the honest fix for a mistaken completion
    (keeps id + history instead of re-adding a lookalike). Returns the description,
    or None if the id doesn't exist or is already open."""
    pred, sp = _scope()
    conn = _conn()
    row = conn.execute(
        f"SELECT description, status FROM commitments WHERE id = ? "
        f"AND status IN ('done', 'dropped') AND {pred}", [commitment_id, *sp]).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute(
        "UPDATE commitments SET status = 'open', completed_at = NULL WHERE id = ?",
        (commitment_id,))
    conn.commit()
    conn.close()
    log_event(commitment_id, 'reopened', f"was {row[1]}")
    return row[0]


def get_open_commitments():
    """All open commitments, dated ones first (soonest due), undated last."""
    pred, sp = _scope()
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' AND {pred} "
        "ORDER BY due_date IS NULL, due_date ASC, id ASC", sp).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def get_due_commitments(today=None):
    """Open NON-recurring commitments due today or overdue. Recurring reminders are
    excluded — they fire as pings on their own schedule and are never 'overdue'."""
    today = today or datetime.now().strftime('%Y-%m-%d')
    pred, sp = _scope()
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' AND recurring IS NULL "
        f"AND due_date IS NOT NULL AND due_date <= ? AND {pred} ORDER BY due_date ASC",
        [today, *sp]).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def _normalize_subject(s):
    """Lowercase and strip stacked Re:/Fwd: prefixes for thread matching."""
    s = (s or '').lower().strip()
    while re.match(r'^(re|fwd|fw)\s*:\s*', s):
        s = re.sub(r'^(re|fwd|fw)\s*:\s*', '', s)
    return s.strip()


def open_reply_owed_for(who, subject):
    """The open reply_owed for this person + thread (normalized subject), or None.
    NOT for blind dedup — a thread legitimately re-owes a reply each time the other
    person writes back. The digest uses this to decide whether the prior obligation was
    already satisfied (you replied) before opening a new one for a fresh inbound."""
    target = _normalize_subject(subject)
    who_l = (who or '').lower()
    for c in get_open_commitments():
        if c['kind'] != 'reply_owed' or (c['who'] or '').lower() != who_l:
            continue
        existing_subject = c['description'].split(': ', 1)[-1]  # after "Reply to X:"
        if _normalize_subject(existing_subject) == target:
            return c
    return None


def has_open_reply_owed(who, subject):
    return open_reply_owed_for(who, subject) is not None


def get_upcoming_commitments(days=7, today=None):
    """Open commitments due within the next `days` days (excluding today/overdue)."""
    today = today or datetime.now().strftime('%Y-%m-%d')
    horizon = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=days)).strftime('%Y-%m-%d')
    pred, sp = _scope()
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' AND due_date > ? AND due_date <= ? AND {pred} "
        "ORDER BY due_date ASC", [today, horizon, *sp]).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


RECURRENCES = {'daily', 'weekly', 'monthly', 'yearly'}  # plus 'every_N_days'
DEFAULT_PING_HOUR = 9  # recurring reminders with no specific time ping mid-morning


def normalize_recurrence(rec):
    """Accept 'daily'/'weekly'/'monthly'/'yearly' or 'every_N_days'; else None."""
    if not rec:
        return None
    rec = rec.strip().lower()
    if rec in RECURRENCES:
        return rec
    m = re.match(r'every[_ ](\d+)[_ ]days?', rec)
    if m:
        return f"every_{int(m.group(1))}_days"
    return None


def _next_date(date_iso, recurrence):
    """The next occurrence of date_iso under recurrence (one step forward)."""
    d = datetime.strptime(date_iso, '%Y-%m-%d')
    if recurrence == 'daily':
        return (d + timedelta(days=1)).strftime('%Y-%m-%d')
    if recurrence == 'weekly':
        return (d + timedelta(days=7)).strftime('%Y-%m-%d')
    if recurrence == 'yearly':
        return f"{d.year + 1}{date_iso[4:]}"
    if recurrence == 'monthly':
        month = d.month % 12 + 1
        year = d.year + (1 if d.month == 12 else 0)
        day = min(d.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30,
                          31, 31, 30, 31, 30, 31][month - 1])
        return f"{year:04d}-{month:02d}-{day:02d}"
    m = re.match(r'every_(\d+)_days', recurrence or '')
    if m:
        return (d + timedelta(days=int(m.group(1)))).strftime('%Y-%m-%d')
    return date_iso


def advance_recurring(commitment_id, today=None):
    """Roll a recurring commitment's due_date forward to the next FUTURE occurrence
    (skips past missed periods in one jump, so a long-offline gap = one catch-up
    ping, not a burst). No-op for non-recurring."""
    today = today or datetime.now().strftime('%Y-%m-%d')
    pred, sp = _scope()
    conn = _conn()
    row = conn.execute(f"SELECT recurring, due_date FROM commitments WHERE id = ? AND {pred}",
                       [commitment_id, *sp]).fetchone()
    if not row or not row[0] or not row[1]:
        conn.close()
        return
    recurrence, due = row
    guard = 0
    while due <= today and guard < 500:
        due = _next_date(due, recurrence)
        guard += 1
    conn.execute("UPDATE commitments SET due_date = ? WHERE id = ?", (due, commitment_id))
    conn.commit()
    conn.close()


def get_timed_due_now(now=None):
    """Open commitments scheduled for a specific time today whose moment has arrived."""
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    hhmm = now.strftime('%H:%M')
    pred, sp = _scope()
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' AND due_date = ? AND due_time IS NOT NULL "
        f"AND due_time <= ? AND {pred}", [today, hhmm, *sp]).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def get_pingable_now(now=None):
    """Everything that should ping right now: timed commitments at their time, and
    recurring reminders whose occurrence has arrived (mid-morning if no time set).
    Recurring entries carry a True 'is_recurring' so the caller can advance them."""
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    hhmm = now.strftime('%H:%M')
    pred, sp = _scope()
    conn = _conn()
    rows = conn.execute(
        f"{_SELECT} WHERE status = 'open' AND due_date IS NOT NULL AND due_date <= ? "
        f"AND (due_time IS NOT NULL OR recurring IS NOT NULL) AND {pred}", [today, *sp]).fetchall()
    conn.close()
    out = []
    for c in _rows_to_dicts(rows):
        if c['due_time']:
            ready = (c['due_date'] < today) or (c['due_time'] <= hhmm)
        else:  # recurring, no specific time
            ready = now.hour >= DEFAULT_PING_HOUR
        if ready:
            c['is_recurring'] = bool(c['recurring'])
            out.append(c)
    return out


def resolve_replied(reply_checker):
    """Auto-close reply_owed commitments the user has actually answered. `reply_checker`
    is injected (email_manager.user_has_replied) to avoid a skill→skill import cycle;
    it takes (subject, since_date) and returns True if the user replied. Uses each
    commitment's created_at as the 'since' (so we only count replies sent after it was
    tracked). Returns the descriptions of the ones resolved."""
    resolved = []
    for c in get_open_commitments():
        if c['kind'] != 'reply_owed':
            continue
        subject = c['description'].split(': ', 1)[-1]
        since = c.get('created_at') or None  # full timestamp → epoch-precise reply check
        try:
            if reply_checker(subject, since):
                complete(c['id'])
                resolved.append(c['description'])
        except Exception as e:
            print(f"[reply-resolve] check failed for #{c['id']}: {e}")
    return resolved


def complete(commitment_id):
    """Mark done. Recurring commitments roll to their next occurrence instead of closing."""
    pred, sp = _scope()
    conn = _conn()
    row = conn.execute(
        f"SELECT description, recurring, due_date FROM commitments WHERE id = ? "
        f"AND status = 'open' AND {pred}", [commitment_id, *sp]).fetchone()
    if not row:
        conn.close()
        return None
    description, recurring, due_date = row
    if recurring and due_date:
        next_due = _next_date(due_date, recurring)
        conn.execute("UPDATE commitments SET due_date = ? WHERE id = ?",
                     (next_due, commitment_id))
        result = f"'{description}' done for now — next {recurring.replace('_', ' ')}: {next_due}."
        conn.commit()
        conn.close()
        log_event(commitment_id, 'completed', f"recurring; rolled to {next_due}")
    else:
        conn.execute(
            "UPDATE commitments SET status = 'done', completed_at = ? WHERE id = ?",
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), commitment_id))
        result = f"'{description}' marked done."
        conn.commit()
        conn.close()
        late = ""
        if due_date and due_date < datetime.now().strftime('%Y-%m-%d'):
            late = f"; {(datetime.now().date() - datetime.fromisoformat(due_date).date()).days}d late"
        log_event(commitment_id, 'completed', f"due {due_date or 'undated'}{late}")
    return result


def drop(commitment_id):
    pred, sp = _scope()
    conn = _conn()
    cursor = conn.execute(
        f"UPDATE commitments SET status = 'dropped' WHERE id = ? AND status = 'open' AND {pred}",
        [commitment_id, *sp])
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    if changed:
        log_event(commitment_id, 'dropped')
    return changed > 0


def format_line(c, today=None):
    """One human-readable line for briefings/digests/chat lists."""
    today = today or datetime.now().strftime('%Y-%m-%d')
    parts = [f"#{c['id']} {c['description']}"]
    if c['who'] and c['who'] not in c['description']:
        parts.append(f"({c['who']})")
    if c['due_date']:
        if c.get('recurring'):
            # Recurring items are never "overdue" — show the next future occurrence.
            nxt, guard = c['due_date'], 0
            while nxt < today and guard < 500:
                nxt, guard = _next_date(nxt, c['recurring']), guard + 1
            parts.append(f"— repeats {c['recurring'].replace('_', ' ')} (next {nxt})")
        else:
            when = c['due_date'] + (f" {c['due_time']}" if c['due_time'] else "")
            marker = " ⚠️ OVERDUE" if c['due_date'] < today else ""
            parts.append(f"— due {when}{marker}")
    return " ".join(parts)


# --- Agent tools ---

@tool
def add_commitment(description: str, kind: str = 'promise', who: str = None,
                   due_date_iso: str = None, due_time: str = None,
                   recurrence: str = None, config: RunnableConfig = None) -> str:
    """Track something the user has committed to or must not forget. BE PROACTIVE:
    when the user mentions a promise, a deadline, someone's birthday, a reply owed, or
    asks to be reminded of something repeatedly — even in passing — track it.

    Args:
        description: What needs to happen (e.g. "Reply to Rohan", "Renew passport",
            "Check in with HubSpot on my PERM filing").
        kind: One of 'reply_owed', 'deadline', 'people_date', 'promise'.
        who: The person/org involved, if any.
        due_date_iso: Due date YYYY-MM-DD (compute from "tomorrow"/"Friday"). For a
            recurring reminder this is the FIRST occurrence. Omit if genuinely undated.
        due_time: HH:MM 24h — ONLY when a specific time is named. Timed/recurring
            commitments ping; date-only non-recurring ones surface in the briefing.
        recurrence: makes it a REPEATING reminder that re-fires on a schedule until
            dropped. One of 'daily', 'weekly', 'monthly', 'yearly', or 'every_N_days'
            (e.g. 'every_3_days'). Use 'yearly' for birthdays. A weekly check-in =
            recurrence='weekly' with due_date_iso set to the first one.
    """
    tok, refusal = scope_from_config(config)
    if refusal:
        return refusal
    try:
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
        rec = normalize_recurrence(recurrence)
        if recurrence and not rec:
            return (f"Error: recurrence must be daily/weekly/monthly/yearly/every_N_days, "
                    f"got {recurrence!r}.")
        if rec and not due_date_iso:
            return "Error: a recurring reminder needs a first due_date_iso."
        cid = add(description, kind=kind, who=who, due_date=due_date_iso, due_time=due_time,
                  recurring=rec)
        when = f" for {due_date_iso}{' ' + due_time if due_time else ''}" if due_date_iso else ""
        rec_note = f", repeating {rec.replace('_', ' ')}" if rec else ""
        return f"Tracked commitment #{cid}: '{description}'{when}{rec_note}."
    finally:
        if tok:
            reset_current_user(tok)


@tool
def list_commitments(include_upcoming_only: bool = False, config: RunnableConfig = None) -> str:
    """Show the user's open commitments. Use when they ask what they owe, what's
    pending, what's due, or what's on their plate.

    Args:
        include_upcoming_only: True to show only items due in the next 7 days.
    """
    tok, refusal = scope_from_config(config)
    if refusal:
        return refusal
    try:
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
    finally:
        if tok:
            reset_current_user(tok)


@tool
def complete_commitment(commitment_id: int, config: RunnableConfig = None) -> str:
    """Mark a commitment as done when the user says they did it. Use the #id from
    list_commitments. Yearly dates (birthdays) roll to next year automatically."""
    tok, refusal = scope_from_config(config)
    if refusal:
        return refusal
    try:
        result = complete(commitment_id)
        return result or f"No open commitment with id {commitment_id}."
    finally:
        if tok:
            reset_current_user(tok)


@tool
def drop_commitment(commitment_id: int, config: RunnableConfig = None) -> str:
    """Drop a commitment the user no longer intends to do (without marking it done)."""
    tok, refusal = scope_from_config(config)
    if refusal:
        return refusal
    try:
        return (f"Dropped commitment #{commitment_id}." if drop(commitment_id)
                else f"No open commitment with id {commitment_id}.")
    finally:
        if tok:
            reset_current_user(tok)


@tool
def reschedule_commitment(commitment_id: int, new_due_date_iso: str,
                          new_due_time: str = None, config: RunnableConfig = None) -> str:
    """Move a commitment to a new date (and optional HH:MM time) when the user says to
    push/move/postpone it ("push it to Friday", "make it next week"). Keeps the item and
    its history — NEVER drop + re-add to reschedule. Clears any snooze.

    Args:
        commitment_id: The #id from list_commitments.
        new_due_date_iso: New date YYYY-MM-DD (compute from words like "Friday").
        new_due_time: HH:MM 24h — only when they name a time.
    """
    try:
        datetime.strptime(new_due_date_iso, '%Y-%m-%d')
        if new_due_time:
            datetime.strptime(new_due_time, '%H:%M')
    except ValueError as e:
        return f"Error: bad date/time ({e}). Use YYYY-MM-DD and HH:MM."
    tok, refusal = scope_from_config(config)
    if refusal:
        return refusal
    try:
        desc = reschedule(commitment_id, new_due_date_iso, new_due_time)
        if not desc:
            return f"No open commitment with id {commitment_id}."
        pushes = slip_counts([commitment_id]).get(commitment_id, 0)
        note = f" (that's push #{pushes} for this one)" if pushes >= 3 else ""
        when = new_due_date_iso + (f" {new_due_time}" if new_due_time else "")
        return f"Rescheduled '{desc}' to {when}.{note}"
    finally:
        if tok:
            reset_current_user(tok)


@tool
def snooze_commitment(commitment_id: int, until_date_iso: str,
                      config: RunnableConfig = None) -> str:
    """Silence nudges/chasing about a commitment until a date, when the user says to stop
    reminding them for now ("stop bugging me about this until next week"). The item stays
    open and listed — only proactive pings pause.

    Args:
        commitment_id: The #id from list_commitments.
        until_date_iso: Resume nudging on this date, YYYY-MM-DD.
    """
    try:
        datetime.strptime(until_date_iso, '%Y-%m-%d')
    except ValueError:
        return f"Error: until_date_iso must be YYYY-MM-DD, got {until_date_iso}."
    tok, refusal = scope_from_config(config)
    if refusal:
        return refusal
    try:
        desc = snooze(commitment_id, until_date_iso)
        return (f"Snoozed '{desc}' — I'll stay quiet about it until {until_date_iso}."
                if desc else f"No open commitment with id {commitment_id}.")
    finally:
        if tok:
            reset_current_user(tok)


@tool
def reopen_commitment(commitment_id: int, config: RunnableConfig = None) -> str:
    """Reopen a commitment that was completed or dropped by mistake. THE fix for a wrong
    completion — restores the same item with its history intact (never re-add a copy)."""
    tok, refusal = scope_from_config(config)
    if refusal:
        return refusal
    try:
        desc = reopen(commitment_id)
        return (f"Reopened #{commitment_id}: '{desc}' — back on the list."
                if desc else f"#{commitment_id} isn't a completed/dropped commitment I can reopen.")
    finally:
        if tok:
            reset_current_user(tok)


def commitment_patterns(today=None) -> dict:
    """Mine the commitments table for patterns over time — deterministic, no LLM. What's
    slipping (open + past due), where slippage concentrates (which people / kinds), and how
    often completed items landed late. Returns the raw stats plus a `findings` list of
    human-readable sentences (only genuinely notable ones)."""
    from collections import Counter
    today = today or datetime.now().date()

    pred, sp = _scope()
    conn = _conn()
    rows = conn.execute(
        "SELECT id, description, kind, who, due_date, status, completed_at FROM commitments "
        f"WHERE {pred}", sp).fetchall()
    conn.close()

    def _d(s):
        try:
            return datetime.fromisoformat((s or "")[:10]).date()
        except Exception:
            return None

    overdue, late, done_with_due, open_count = [], 0, 0, 0
    by_who, by_kind = Counter(), Counter()
    for cid, desc, kind, who, due, status, completed in rows:
        dd = _d(due)
        if status == 'open':
            open_count += 1
            if dd and dd < today:
                overdue.append({"id": cid, "description": desc, "who": who, "kind": kind,
                                "days_overdue": (today - dd).days})
                if who:
                    by_who[who] += 1
                by_kind[kind] += 1
        elif status == 'done' and dd:
            done_with_due += 1
            cd = _d(completed)
            if cd and cd > dd:
                late += 1

    overdue.sort(key=lambda x: -x["days_overdue"])
    findings = []
    if overdue:
        o = overdue[0]
        findings.append(f"{len(overdue)} open commitment(s) overdue — oldest: "
                        f"“{o['description'][:50]}” ({o['days_overdue']}d past due).")
    for who, n in by_who.most_common(2):
        if n >= 2:
            findings.append(f"{n} overdue commitments involve {who} — a recurring slip.")
    for kind, n in by_kind.most_common(1):
        if n >= 3:
            findings.append(f"Your “{kind}” commitments pile up — {n} are overdue.")
    if done_with_due >= 4 and late:
        findings.append(f"{late} of {done_with_due} completed commitments finished late.")

    # Serial rescheduling (from the event log): items pushed 3+ times are avoidance,
    # not scheduling — name them.
    open_ids = [r[0] for r in rows if r[5] == 'open']
    serial = {cid: n for cid, n in slip_counts(open_ids).items() if n >= 3}
    desc_by_id = {r[0]: r[1] for r in rows}
    for cid, n in sorted(serial.items(), key=lambda kv: -kv[1])[:2]:
        findings.append(f"“{desc_by_id.get(cid, f'#{cid}')[:50]}” has been rescheduled "
                        f"{n} times — pick a real date or let it go?")

    return {"overdue": overdue, "late_completions": late, "done_with_due": done_with_due,
            "overdue_by_who": dict(by_who), "overdue_by_kind": dict(by_kind),
            "open_count": open_count, "serial_reschedules": serial, "findings": findings}


@tool
def analyze_commitments() -> str:
    """Surface PATTERNS across the user's commitments over time — what's slipping (overdue),
    where it concentrates (which people/kinds), whether things finish late. Use when they ask
    how they're doing on commitments, what keeps slipping, or for a review."""
    p = commitment_patterns()
    if not p["findings"]:
        return "Nothing notable — nothing's overdue and recent commitments closed on time."
    return "Patterns I see across your commitments:\n" + "\n".join(f"• {f}" for f in p["findings"])
