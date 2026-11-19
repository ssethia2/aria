"""The matters in motion in his life, and where each one stands now.

Aria's background side is a set of narrow judges, each shown one slice: the email that just
arrived, the list of open commitments, one calendar event. None of them held the picture
("the Acme process is past the technical round; the onsite is theirs to schedule"), so
each re-read raw mail and went stale its own way: a digest replayed yesterday's "your
interview is today", a nudge pushed a flight that was a friend's, a wait stayed open after
the answer came. A matter is that picture, kept current in one place:

    title       what it is ("Acme interview process", "Porto trip (Dec 3)")
    owner       None when it is his; else whose it is (a friend's booking he forwarded)
    stage       where it stands now
    latest      the newest thing that happened, with its day
    next_step   what happens next, and who does it
    move        'his' | 'theirs' | 'none'
    next_date   when the next step is, only if the mail or calendar states it
    people      who he deals with on it
    items       ids of his open commitments that are parts of it

Every new email, received or sent, is folded in by one judge (`UPDATE_PROMPT`, judge
"matters" in engine.JUDGES); the model proposes and `apply` checks: it must cite mail it was
shown, touch a matter that exists, quote any date, and close only with mail behind it. The
chat agent corrects it when he does ("that flight is Kim's"). Judges read `render()`.

    python3 matters.py                    # what is in motion now
    python3 matters.py --history 4        # how matter M4 got where it is
    python3 matters.py --seed --days 21   # build the picture from recent mail (once)

Stored beside his commitments in aria_calendar.db (tables matters, matter_events).
"""
import json
import re
import sys
from datetime import datetime, timedelta

FIELDS = ('title', 'owner', 'stage', 'latest', 'next_step', 'move', 'next_date', 'people')
LIMITS = {'title': 80, 'owner': 40, 'stage': 200, 'latest': 300, 'next_step': 220,
          'people': 200}
_MOVE_TAG = re.compile(r"\s*\((?:his|their|theirs)(?: move)?\)\s*$", re.I)
MOVES = ('his', 'theirs', 'none')
CLOSED_SHOWN_DAYS = 7       # recently closed matters stay visible, so news isn't a "new" one
DATE_HORIZON_DAYS = 365

# How the judge says "his own": anything here, or his own name, means no other owner.
_HIS = {'', 'his', 'him', 'he', 'me', 'mine', 'user', 'the user', 'null', 'none', 'self'}


def _conn():
    from skills import commitment_manager
    conn = commitment_manager._conn()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS matters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            owner TEXT,
            stage TEXT,
            latest TEXT,
            next_step TEXT,
            move TEXT,
            next_date TEXT,
            people TEXT,
            items TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT,
            tenant TEXT
        )''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS matter_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            matter_id INTEGER NOT NULL,
            at TEXT NOT NULL,
            source TEXT NOT NULL,     -- mail | sent | chat | seed
            ref TEXT,                 -- the email ids it came from
            detail TEXT               -- JSON: the fields that changed, or the reason
        )''')
    return conn


def _tenant():
    from tenant import get_current_user
    return get_current_user()


def _stamp(now: datetime = None) -> str:
    return (now or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')


_COLS = ('id', 'title', 'owner', 'stage', 'latest', 'next_step', 'move', 'next_date',
         'people', 'items', 'status', 'created_at', 'updated_at', 'closed_at')


def _rows(rows) -> list:
    out = []
    for r in rows:
        m = dict(zip(_COLS, r))
        m['items'] = json.loads(m['items'] or '[]')
        out.append(m)
    return out


def open_matters() -> list:
    """Open matters, most recently moved first."""
    conn = _conn()
    rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM matters WHERE status = 'open' "
                        f"AND tenant = ? ORDER BY updated_at DESC, id DESC",
                        [_tenant()]).fetchall()
    conn.close()
    return _rows(rows)


def recently_closed(days: int = CLOSED_SHOWN_DAYS, now: datetime = None) -> list:
    since = _stamp((now or datetime.now()) - timedelta(days=days))
    conn = _conn()
    rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM matters WHERE status = 'closed' "
                        f"AND tenant = ? AND closed_at >= ? ORDER BY closed_at DESC",
                        [_tenant(), since]).fetchall()
    conn.close()
    return _rows(rows)


def get(mid: int):
    conn = _conn()
    row = conn.execute(f"SELECT {', '.join(_COLS)} FROM matters WHERE id = ? AND tenant = ?",
                       [mid, _tenant()]).fetchone()
    conn.close()
    return _rows([row])[0] if row else None


def _log(conn, mid, source, ref, detail, now=None):
    conn.execute("INSERT INTO matter_events (matter_id, at, source, ref, detail) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (mid, _stamp(now), source, ref,
                  detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)))


def events(mid: int) -> list:
    conn = _conn()
    rows = conn.execute("SELECT at, source, ref, detail FROM matter_events "
                        "WHERE matter_id = ? ORDER BY id", [mid]).fetchall()
    conn.close()
    return [dict(zip(('at', 'source', 'ref', 'detail'), r)) for r in rows]


def create(fields: dict, items=(), source='chat', ref=None, now=None) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO matters (title, owner, stage, latest, next_step, move, next_date, people, "
        "items, status, created_at, updated_at, tenant) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
        tuple(fields.get(k) for k in FIELDS)
        + (json.dumps(sorted(set(items))), _stamp(now), _stamp(now), _tenant()))
    mid = cur.lastrowid
    _log(conn, mid, source, ref, {k: v for k, v in fields.items() if v} | {'created': True},
         now)
    conn.commit()
    conn.close()
    return mid


def update(mid: int, fields: dict, items=None, source='chat', ref=None, now=None) -> dict:
    """Set the given fields (None clears one); returns what actually changed."""
    current = get(mid)
    if not current or current['status'] != 'open':
        return {}
    changed = {k: v for k, v in fields.items() if k in FIELDS and current.get(k) != v}
    if items is not None and sorted(set(items)) != current['items']:
        changed['items'] = sorted(set(items))
    if not changed:
        return {}
    conn = _conn()
    sets = ", ".join(f"{k} = ?" for k in changed)
    values = [json.dumps(v) if k == 'items' else v for k, v in changed.items()]
    conn.execute(f"UPDATE matters SET {sets}, updated_at = ? WHERE id = ?",
                 values + [_stamp(now), mid])
    _log(conn, mid, source, ref, changed, now)
    conn.commit()
    conn.close()
    return changed


def close(mid: int, why: str, source='chat', ref=None, now=None) -> bool:
    conn = _conn()
    cur = conn.execute("UPDATE matters SET status = 'closed', closed_at = ?, updated_at = ? "
                       "WHERE id = ? AND status = 'open' AND tenant = ?",
                       (_stamp(now), _stamp(now), mid, _tenant()))
    if cur.rowcount:
        _log(conn, mid, source, ref, {'closed': why}, now)
    conn.commit()
    conn.close()
    return bool(cur.rowcount)


def reopen(mid: int, source='chat', now=None) -> bool:
    conn = _conn()
    cur = conn.execute("UPDATE matters SET status = 'open', closed_at = NULL, updated_at = ? "
                       "WHERE id = ? AND status = 'closed' AND tenant = ?",
                       (_stamp(now), mid, _tenant()))
    if cur.rowcount:
        _log(conn, mid, source, None, {'reopened': True}, now)
    conn.commit()
    conn.close()
    return bool(cur.rowcount)


def matter_of(commitment_id: int, among: list = None):
    """The open matter a commitment is part of, or None."""
    for m in among if among is not None else open_matters():
        if commitment_id in m['items']:
            return m
    return None


# ---- what the judges read ---------------------------------------------------------------

def _day(stamp: str) -> str:
    try:
        d = datetime.strptime(stamp[:10], '%Y-%m-%d')
        return f"{d.strftime('%a %b')} {d.day}"
    except (TypeError, ValueError):
        return '?'


def render_one(m: dict) -> str:
    whose = f" — {m['owner']}'s, not his" if m.get('owner') else ""
    lines = [f"M{m['id']} {m['title']}{whose}"]
    if m.get('stage'):
        lines.append(f"   where it stands: {m['stage']}")
    if m.get('latest'):
        lines.append(f"   latest: {m['latest']}")
    nxt = m.get('next_step') or ''
    if m.get('move') == 'theirs':
        nxt += " (their move)" if nxt else "their move"
    elif m.get('move') == 'his':
        nxt += " (his move)" if nxt else "his move"
    if m.get('next_date'):
        nxt += f", {_day(m['next_date'])}"
    if nxt:
        lines.append(f"   next: {nxt}")
    tail = []
    if m.get('people'):
        tail.append(f"with: {m['people']}")
    if m['items']:
        tail.append("his items: " + ", ".join(f"#{i}" for i in m['items']))
    tail.append(f"updated {_day(m['updated_at'])}")
    lines.append("   " + " · ".join(tail))
    return "\n".join(lines)


def render(items: list = None, closed: list = None) -> str:
    """The matters as prompt text. A matter marked as someone else's is context, never
    his to do."""
    items = open_matters() if items is None else items
    closed = recently_closed() if closed is None else closed
    if not items and not closed:
        return "(none yet)"
    out = "\n".join(render_one(m) for m in items) or "(none open)"
    if closed:
        out += ("\n\nClosed in the last week (over; news about one of these continues it):\n"
                + "\n".join(f"M{m['id']} {m['title']} — closed {_day(m['closed_at'])}"
                            for m in closed))
    return out


GUIDE = """What is in motion in his life, one matter each: where it stands now, the newest step,
and whose move it is. It is kept current from every email he gets and sends, so it is newer
than any single email, note or commitment: where they disagree, it wins. A matter marked as
someone else's is theirs: never present it as his ("your flight", "your account"); only his
part in it is his (his items in it, or a next step that is his move). A matter whose move is
"theirs" is not his to act on."""


def for_prompt() -> str:
    """The matters with how to read them, for any judge's prompt. Never raises."""
    try:
        return f"{GUIDE}\n\n{render()}"
    except Exception as e:
        print(f"[matters] unavailable: {e}")
        return "(not available)"


# ---- folding new mail in ----------------------------------------------------------------

UPDATE_PROMPT = """You keep Aria's picture of what is going on in her user's life: the MATTERS in
motion. A matter is one thing that unfolds over days or weeks and involves him: an
application or interview process, a trip, an appointment being arranged, a purchase, return
or repair, a bill or account problem, a plan with friends or family, a form or case with a
government office or bank. Each email below is one step in some matter. Fold the new emails
into the matters, so each one says where it stands NOW.

For each email:
- It continues an open or recently closed matter (same thread, same company, person, trip
  or case): update that matter. This is the common case; prefer it to starting a new one.
- It starts something that will involve him over the coming days: a new matter.
- It is noise for this purpose (a newsletter, a promotion, a receipt or notice with nothing
  pending, a one-time code on its own, a social notification): leave it out.

For each matter you touch, give its current state:
- title: what it is, specific ("Acme interview process", "Porto trip (Dec 3)").
- owner: null when it is his own. When the mail or what you remember shows it is someone
  else's (a booking in another passenger's name, something he is arranging for a friend or
  family member, an account that isn't his), that person's name. Keep an owner already set
  unless the mail clearly says otherwise. His part in someone else's matter (replying to
  them, booking for them) still goes in next_step and move.
- stage: where it stands now, in a few words.
- latest: the newest thing that happened, in one sentence starting with its day ("Wed Nov
  18: he told Sam he can come in after Dec 30").
- next_step: what happens next, and who does it, in one sentence.
- move: "his" when the next step is his, "theirs" when he is waiting on someone, "none"
  when nothing is pending.
- next_date: the date of the next step (YYYY-MM-DD) only when the mail or his calendar
  states it, with date_quote: the words that state it, copied exactly. Else both null.
- people: the people and organisations he deals with on it.
- commitments: the ids of his open commitments that are parts of this matter.
- closed: true only when the mail shows it is over (the trip happened, the process ended,
  the order arrived and nothing is pending), with "why".

A later email overrides an earlier one: the matter says how things stand after the newest.
Write about him in the third person, plainly. Never state anything that isn't in the
emails, the matters, his calendar or his commitments.

Today: {today}

His standing rules:
{rules}

Matters:
{matters}

His open commitments:
{commitments}

His calendar:
{calendar}

New emails, oldest first ("received", or "sent" by him) (JSON):
{emails}

Return ONLY a raw JSON array (no markdown), one object per matter that changes:
[{{"matter": <the M number of the matter as a number, or "new">, "email_ids": ["<ids of the emails this comes from>"], "title": "...", "owner": null or "<name>", "stage": "...", "latest": "...", "next_step": "...", "move": "his" or "theirs" or "none", "next_date": "YYYY-MM-DD" or null, "date_quote": "<words>" or null, "people": ["..."], "commitments": [<ids>], "closed": false, "why": null}}]
Return [] if no email changes any matter."""


def _clean(value, limit: int) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v)
    text = " ".join(str(value or '').split())
    if len(text) > limit:
        text = text[:limit - 1].rsplit(' ', 1)[0] + "…"
    return text


def _is_him(owner: str) -> bool:
    plain = owner.strip().lower()
    if plain in _HIS:
        return True
    words = lambda s: {w for w in re.findall(r'[a-z]+', s.lower()) if len(w) > 2}  # noqa: E731
    try:
        import moments
        me = words(moments.my_name() or '')
    except Exception:
        me = set()
    return bool(me and words(plain) and words(plain) <= me)


def _checked_date(change: dict, evidence: str, now: datetime):
    from engine import stated_date
    if not change.get('next_date'):
        return None
    return stated_date({'date': change.get('next_date'), 'quote': change.get('date_quote')},
                       evidence, now, horizon_days=DATE_HORIZON_DAYS)


def apply(verdict, emails: list, open_ids: set, now: datetime = None, calendar: str = "",
          source: str = 'mail') -> list:
    """Apply the judge's proposed changes after checking each one; returns what was done,
    one line each, for the decision record. A change must cite mail it was shown and an
    open (or recently closed) matter; a date stands only if its words are in that mail or
    on his calendar; a close needs mail behind it."""
    import decisions
    now = now or datetime.now()
    by_id = {str(e.get('id')): e for e in emails}
    current = {m['id']: m for m in open_matters()}
    closed = {m['id']: m for m in recently_closed(now=now)}
    done = []

    def note(text):
        decisions.note("matters", text, now)
        done.append(text)

    for ch in verdict if isinstance(verdict, list) else []:
        if not isinstance(ch, dict):
            continue
        cited = [by_id[str(i)] for i in ch.get('email_ids') or [] if str(i) in by_id]
        if not cited:
            note(f"ignored a change to {ch.get('matter')!r}: it cites no email it was shown")
            continue
        ref = ",".join(str(e['id']) for e in cited)
        target = ch.get('matter')
        try:
            target = int(str(target).lstrip('Mm#')) if str(target).lower() != 'new' else 'new'
        except ValueError:
            note(f"ignored a change to unknown matter {target!r}")
            continue
        if target in closed and target not in current:
            reopen(target, source=source, now=now)        # news on a matter just closed
            current[target] = get(target)
        if target != 'new' and target not in current:
            note(f"ignored a change to M{target}: no such open matter")
            continue

        fields = {k: _clean(ch.get(k), LIMITS[k]) for k in ('title', 'stage', 'latest',
                                                             'next_step', 'people')
                  if ch.get(k)}
        if fields.get('next_step'):     # whose move is its own field; render() says it
            fields['next_step'] = _MOVE_TAG.sub('', fields['next_step'])
        owner = _clean(ch.get('owner'), LIMITS['owner'])
        if owner and not _is_him(owner):
            fields['owner'] = owner
        elif target == 'new' or source == 'chat':
            fields['owner'] = None
        # Mail never takes an owner away: "that flight is Kim's" is his word, and the
        # judge's routine null on the next email must not undo it.
        if ch.get('move') in MOVES:
            fields['move'] = ch['move']
        evidence = " ".join(" ".join(str(e.get(k) or '') for k in
                                     ('subject', 'snippet', 'text')) for e in cited)
        date = _checked_date(ch, evidence + " " + calendar, now)
        if ch.get('next_date') and not date:
            note(f"dropped the date {ch.get('next_date')!r} for {fields.get('title') or target}: "
                 f"its words aren't in the mail or calendar, or it's out of range")
        if date or 'next_date' in ch:
            fields['next_date'] = date

        items = None
        if isinstance(ch.get('commitments'), list):
            wanted = set()
            for i in ch['commitments']:
                try:
                    wanted.add(int(str(i).lstrip('#')))
                except ValueError:
                    pass
            wanted &= set(open_ids)
            kept = set(current[target]['items']) & set(open_ids) if target != 'new' else set()
            items = kept | wanted
            for other in current.values():          # an item is part of one matter
                if other['id'] != target and set(other['items']) & wanted:
                    update(other['id'], {}, items=set(other['items']) - wanted,
                           source=source, ref=ref, now=now)
                    other['items'] = sorted(set(other['items']) - wanted)

        if target == 'new':
            if not fields.get('title'):
                note("ignored a new matter with no title")
                continue
            if ch.get('closed'):
                continue                             # nothing in motion to keep
            mid = create(fields, items or (), source=source, ref=ref, now=now)
            current[mid] = get(mid)
            note(f"new M{mid} {fields['title']!r}"
                 + (f" ({fields['owner']}'s)" if fields.get('owner') else "")
                 + f" from {ref}")
            continue

        changed = update(target, fields, items=items, source=source, ref=ref, now=now)
        if changed:
            current[target] = get(target)
            note(f"M{target} {current[target]['title']!r}: "
                 + ", ".join(f"{k} → {v!r}" for k, v in changed.items())[:400])
        if ch.get('closed'):
            why = _clean(ch.get('why'), 160) or "the mail shows it is over"
            if close(target, why, source=source, ref=ref, now=now):
                current.pop(target, None)
                note(f"closed M{target}: {why}")
    return done


# ---- building the picture once, from recent mail ----------------------------------------

def _recent_mail(service, days: int, most: int = 400) -> list:
    """His recent mail both ways, oldest first, without bulk mail. Snippets only."""
    import situation
    api = service.users().messages()
    q = (f'newer_than:{days}d {{in:inbox in:sent}} -category:promotions -category:social '
         f'-subject:"Daily Assistant Summary"')
    ids, token = [], None
    while len(ids) < most:
        resp = api.list(userId='me', q=q, maxResults=100, pageToken=token).execute()
        ids += [m['id'] for m in resp.get('messages', [])]
        token = resp.get('nextPageToken')
        if not token:
            break
    msgs = situation._batch(service, {mid: api.get(
        userId='me', id=mid, format='metadata',
        metadataHeaders=['From', 'To', 'Subject', 'List-Unsubscribe']) for mid in ids[:most]})
    out = []
    for mid, msg in msgs.items():
        h = {x['name'].lower(): x['value'] for x in msg.get('payload', {}).get('headers', [])}
        if h.get('list-unsubscribe', '').strip():
            continue                                  # bulk mail, as the screener skips it
        sent = 'SENT' in (msg.get('labelIds') or [])
        ts = int(msg.get('internalDate', 0)) / 1000
        e = {'id': mid, 'direction': 'sent' if sent else 'received',
             'subject': h.get('subject', ''), 'snippet': msg.get('snippet', ''),
             'when': datetime.fromtimestamp(ts).strftime('%a %b %-d, %-I:%M %p'), 'ts': ts}
        e['to' if sent else 'from'] = h.get('to' if sent else 'from', '')
        out.append(e)
    return sorted(out, key=lambda e: e['ts'])


def seed(days: int = 21, batch: int = 15, dry_run: bool = False) -> int:
    """Build the picture from the last `days` of mail, oldest first, so each matter ends
    where the newest mail leaves it. Run once; after that every new email keeps it current."""
    import email_backend
    import engine
    import situation
    from instructions import render_for_prompt
    from skills import commitment_manager
    from skills.email_manager import get_gmail_service
    service = None if email_backend.using_app_password() else get_gmail_service()
    if not service:
        print("No Gmail service; can't read recent mail.")
        return 1
    mail = _recent_mail(service, days)
    chunks = [mail[i:i + batch] for i in range(0, len(mail), batch)]
    print(f"{len(mail)} emails from the last {days} days, in {len(chunks)} batches.")
    if dry_run:
        return 0
    calendar = situation.in_motion()["calendar"]
    rules = render_for_prompt() or "(none)"
    for n, chunk in enumerate(chunks, 1):
        items = commitment_manager.get_open_commitments()
        shown = [{k: v for k, v in e.items() if k != 'ts'} for e in chunk]
        try:
            verdict = engine._parse_llm_json(engine.ask(
                "matters", today=datetime.now().strftime('%A %Y-%m-%d'), rules=rules,
                matters=render(), calendar=calendar,
                commitments="\n".join(commitment_manager.format_line(c) for c in items)
                or "(none)", emails=json.dumps(shown, indent=2, ensure_ascii=False)))
        except Exception as e:
            print(f"  batch {n}: no verdict ({e})")
            continue
        done = apply(verdict, chunk, {c['id'] for c in items}, calendar=calendar,
                     source='seed')
        print(f"  batch {n}/{len(chunks)}: {len(done)} change(s)")
    print(f"\n{len(open_matters())} open matters.")
    return 0


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="The matters in motion in his life.")
    p.add_argument("--seed", action="store_true", help="build them from recent mail")
    p.add_argument("--days", type=int, default=21)
    p.add_argument("--dry-run", action="store_true", help="with --seed: count, don't ask")
    p.add_argument("--history", type=int, metavar="M", help="how matter M got here")
    a = p.parse_args(argv)
    if a.seed:
        return seed(a.days, dry_run=a.dry_run)
    if a.history:
        m = get(a.history)
        if not m:
            print(f"No matter M{a.history}.")
            return 1
        print(render_one(m) + f"\n   status: {m['status']}\n")
        for e in events(a.history):
            print(f"{e['at']}  {e['source']:<5} {e['detail']}")
        return 0
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
