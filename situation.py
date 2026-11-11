"""What his mail and calendar already show about each open commitment.

A commitment row is one moving part of a task. The others live elsewhere: the reply he
sent yesterday, the invite that landed after it, the event now on his calendar. Judging a
row alone, Aria nudged him to "schedule the interview" that was already booked and to
answer a thread he had answered. around() gathers those parts for a set of commitments;
describe() renders them for a prompt or a tool result. enrich_mail() and in_motion() do the
same for anything judging incoming mail: each email gets its thread so far, what she
remembers about the sender and matter, and her notes on the person; the judge also gets his
calendar and open commitments. know() is the general form: everything she has on a
person, company, trip or topic, from every source at once. Read-only, owner-only, and it
never raises: with no Gmail or calendar it returns nothing and callers carry on as before.
"""
import re
import time
from datetime import datetime

WINDOW_DAYS = 21        # how far back mail still counts as part of the same matter
CALENDAR_DAYS = 30
CALENDAR_BACK_DAYS = 21
PER_ITEM = 5            # mail lines shown per commitment
MAX_ITEMS = 60
CACHE_SECONDS = 300     # chat can list commitments several times in a conversation

# Task verbs and filler that say nothing about WHICH matter a commitment is about.
GENERIC_WORDS = {'the', 'and', 'for', 'with', 'from', 'about', 'through', 'out', 'get',
                 'buy', 'sell', 'fix', 'figure', 'finish', 'schedule', 'send', 'reply',
                 'call', 'make', 'book', 'urgent', 'email', 'follow', 'check', 'back',
                 'his', 'her', 'their', 'your', 'that', 'this'}

_cache = {}


def words(text: str) -> set:
    return {w for w in re.findall(r'[a-z0-9]+', (text or '').lower())
            if len(w) >= 3} - GENERIC_WORDS


def search_terms(commitment: dict, all_items: list) -> list:
    """Words to look this commitment up by: the person first, then the words only this
    item has ("acme", not the "interview" three items share)."""
    who = sorted(words(commitment.get('who') or ''))
    others = set().union(*[words(c['description']) for c in all_items
                           if c['id'] != commitment['id']] or [set()])
    own = words(commitment['description'])
    rest = sorted((own - others) or own, key=lambda w: (-len(w), w))
    terms = []
    for w in who + rest:
        if w not in terms:
            terms.append(w)
    return terms[:4]


def _batch(service, requests: dict) -> dict:
    """Run {key: request} in one HTTP round trip; {key: response} for those that worked."""
    got = {}

    def keep(request_id, response, exception):
        if response is not None and not exception:
            got[request_id] = response

    keys = list(requests)
    for i in range(0, len(keys), 50):
        batch = service.new_batch_http_request(callback=keep)
        for k in keys[i:i + 50]:
            batch.add(requests[k], request_id=k)
        batch.execute()
    return got


def _mail_for(service, terms_by_item: dict) -> dict:
    """{item id: recent messages, sent or received, mentioning any of its terms}: the best
    matches, oldest first. Two round trips however many items there are."""
    api = service.users().messages()
    found = _batch(service, {
        str(cid): api.list(userId='me', maxResults=10, q=(
            f'newer_than:{WINDOW_DAYS}d {{{" ".join(terms)}}} '
            f'-subject:"Daily Assistant Summary" -category:promotions'))
        for cid, terms in terms_by_item.items()})
    ids = {cid: [m['id'] for m in found.get(str(cid), {}).get('messages', [])]
           for cid in terms_by_item}
    wanted = sorted({mid for mids in ids.values() for mid in mids})
    msgs = _batch(service, {mid: api.get(userId='me', id=mid, format='metadata',
                                         metadataHeaders=['From', 'To', 'Subject'])
                            for mid in wanted})
    out = {}
    for cid, terms in terms_by_item.items():
        mail = []
        for mid in ids[cid]:
            msg = msgs.get(mid)
            if not msg:
                continue
            h = {x['name']: x['value'] for x in msg.get('payload', {}).get('headers', [])}
            sent = 'SENT' in (msg.get('labelIds') or [])
            entry = {'id': mid, 'direction': 'sent' if sent else 'received',
                     'who': h.get('To' if sent else 'From', ''),
                     'subject': h.get('Subject', ''), 'snippet': msg.get('snippet', ''),
                     'ts': int(msg.get('internalDate', 0)) / 1000}
            text = words(" ".join((entry['who'], entry['subject'], entry['snippet'])))
            entry['hits'] = len(text & set(terms))
            mail.append(entry)
        mail.sort(key=lambda e: (-e['hits'], -e['ts']))
        out[cid] = sorted(mail[:PER_ITEM], key=lambda e: e['ts'])
    return out


def around(commitments: list) -> dict:
    """{"calendar": [event lines, next 30 days], "mail": {commitment id: [messages]}}."""
    # Newest first under the cap: fresh items are the ones still moving.
    items = sorted((c for c in commitments if not c.get('recurring')),
                   key=lambda c: -c['id'])[:MAX_ITEMS]
    key = tuple(sorted(c['id'] for c in items))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    out = {"calendar": [], "mail": {}}
    if not items:
        return out
    try:
        from skills.google_calendar import fetch_events
        # Past weeks too: an interview that already happened still settles the thread
        # that arranged it.
        out["calendar"] = fetch_events(days=CALENDAR_DAYS, dated=True,
                                       back_days=CALENDAR_BACK_DAYS) or []
    except Exception as e:
        print(f"[situation] calendar unavailable: {e}")
    try:
        import email_backend
        from skills.email_manager import get_gmail_service
        service = None if email_backend.using_app_password() else get_gmail_service()
        terms = {c['id']: search_terms(c, items) for c in items}
        if service:
            out["mail"] = _mail_for(service, {cid: t for cid, t in terms.items() if t})
    except Exception as e:
        print(f"[situation] mail unavailable: {e}")
    _cache.clear()
    _cache[key] = (time.time(), out)
    return out


def _mail_line(m: dict) -> str:
    when = datetime.fromtimestamp(m['ts']).strftime('%a %b %-d') if m.get('ts') else '?'
    who = m['who'].split('<')[0].strip(' "') or m['who']
    side = f"HE SENT to {who}" if m['direction'] == 'sent' else f"received from {who}"
    return f"    {when}, {side}: “{m['subject']}” — {m['snippet'][:160]}"


def render(commitments: list, sit: dict) -> str:
    parts = []
    for c in commitments:
        mail = sit["mail"].get(c['id'])
        if mail:
            parts.append(f"  #{c['id']} {c['description']} (tracked since "
                         f"{(c.get('created_at') or '?')[:10]}):\n"
                         + "\n".join(_mail_line(m) for m in mail))
    blocks = []
    if parts:
        blocks.append(f"Mail from the last {WINDOW_DAYS} days that may concern them, "
                      "oldest first:\n" + "\n".join(parts))
    if sit["calendar"]:
        blocks.append(f"On his calendar, past {CALENDAR_BACK_DAYS} days to next "
                      f"{CALENDAR_DAYS} days:\n"
                      + "\n".join(f"  {line}" for line in sit["calendar"]))
    return "\n\n".join(blocks)


def describe(commitments: list) -> str:
    """The mail and calendar around these commitments, as text ("" when there is none)."""
    try:
        return render(commitments, around(commitments))
    except Exception as e:
        print(f"[situation] skipped: {e}")
        return ""


# ---- incoming mail: one context for every judge that reads it ---------------------------

MAIL_GUIDE = """An email is usually one moving part of something already in motion, so read it against
everything you know, not alone:
- "earlier_in_thread" is the conversation so far (his own messages are marked "sent");
- "you_remember" is what your memory holds that may relate (he tells you things in chat:
  who he's interviewing with, trips he's planning, people he's dealing with). Use it only
  when it is clearly about the same person or matter;
- "sender_notes" is what he has told you about this person;
- his calendar and open commitments are listed with the emails.
If the email confirms or continues something already on his calendar or already tracked,
say so ("confirms the Acme interview already on your calendar for Thu 2 PM") and don't ask
for a reply he has already given or an action already done. If it CHANGES something (a new
time, a cancellation, a new ask), say what changed. Mail from a person or company your
memory shows he is actively dealing with matters, however routine it looks."""


def thread_so_far(service, thread_id, message_id=None) -> list:
    """The thread's earlier messages, oldest first: [{"direction", "snippet"}]. A recruiter
    he'd been writing to for a week was digested as "cold outreach he can ignore", and an
    invite answering his own reply read as a fresh ask: each email was judged alone."""
    if not (service and thread_id):
        return []
    try:
        thread = service.users().threads().get(
            userId='me', id=thread_id, format='metadata', metadataHeaders=['From']).execute()
        return [{'direction': 'sent' if 'SENT' in (m.get('labelIds') or []) else 'received',
                 'snippet': (m.get('snippet') or '')[:200]}
                for m in thread.get('messages', []) if m.get('id') != message_id][-4:]
    except Exception:
        return []


def replied_since(service, thread_id, message_id) -> bool:
    """Did he send anything on the thread AFTER this message? A note written when an email
    arrived ("she's waiting on your reply") is stale the moment he answers."""
    if not (service and thread_id and message_id):
        return False
    try:
        msgs = service.users().threads().get(
            userId='me', id=thread_id, format='minimal').execute().get('messages', [])
        ids = [m.get('id') for m in msgs]
        if message_id not in ids:
            return False
        return any('SENT' in (m.get('labelIds') or []) for m in msgs[ids.index(message_id) + 1:])
    except Exception:
        return False


def known_about(sender: str, subject: str) -> dict:
    """What memory and the people notes hold about an email's sender and matter."""
    out = {}
    name = (sender or '').split('<')[0].strip(' "')
    try:
        import memory
        found = memory.recall(f"{name} {subject}", n_results=3)
        if found:
            out['you_remember'] = found[:900]
    except Exception as e:
        print(f"[situation] memory unavailable: {e}")
    try:
        import people
        hit = people._find(people._load(), name) if name else None
        if hit:
            out['sender_notes'] = ", ".join(f"{k}: {v}" for k, v in hit.items() if v)
    except Exception as e:
        print(f"[situation] people notes unavailable: {e}")
    return out


def enrich_mail(emails: list, service=None) -> None:
    """Give each email (in place) its thread so far and what is known about it. Emails
    carry "thread_id" in; it is consumed here."""
    for e in emails:
        earlier = thread_so_far(service, e.pop('thread_id', None), e.get('id'))
        e['he_replied_in_thread'] = any(m['direction'] == 'sent' for m in earlier)
        if earlier:
            e['earlier_in_thread'] = earlier
        e.update(known_about(e.get('sender', ''), e.get('subject', '')))


def in_motion() -> dict:
    """His calendar (next 30 days) and open commitments, as prompt text."""
    calendar, commitments = [], []
    try:
        from skills.google_calendar import fetch_events
        calendar = fetch_events(days=CALENDAR_DAYS, dated=True) or []
    except Exception as e:
        print(f"[situation] calendar unavailable: {e}")
    try:
        from skills.commitment_manager import get_open_commitments, format_line
        commitments = [format_line(c) for c in get_open_commitments()]
    except Exception as e:
        print(f"[situation] commitments unavailable: {e}")
    return {"calendar": "\n".join(calendar) or "(nothing)",
            "commitments": "\n".join(commitments) or "(none)"}


# ---- the general lookup: "what do I know about X?" -------------------------------------

KNOW_MAIL_DAYS = 90
KNOW_BACK_DAYS = 30
KNOW_AHEAD_DAYS = 60


def _mail_about(service, about: str, terms: list) -> list:
    """Recent mail on a subject, sent and received, oldest first. The phrase as he said it
    first (Gmail matches all its words); any single word only if that finds nothing."""
    api = service.users().messages()
    window = (f'newer_than:{KNOW_MAIL_DAYS}d -subject:"Daily Assistant Summary" '
              f'-category:promotions')
    ids = []
    for q in (f'{about} {window}', f'{{{" ".join(terms)}}} {window}'):
        ids = [m['id'] for m in api.list(userId='me', q=q, maxResults=12)
               .execute().get('messages', [])]
        if ids:
            break
    msgs = _batch(service, {mid: api.get(userId='me', id=mid, format='metadata',
                                         metadataHeaders=['From', 'To', 'Subject'])
                            for mid in ids})
    mail = []
    for mid in ids:
        msg = msgs.get(mid)
        if not msg:
            continue
        h = {x['name']: x['value'] for x in msg.get('payload', {}).get('headers', [])}
        sent = 'SENT' in (msg.get('labelIds') or [])
        mail.append({'id': mid, 'direction': 'sent' if sent else 'received',
                     'who': h.get('To' if sent else 'From', ''),
                     'subject': h.get('Subject', ''), 'snippet': msg.get('snippet', ''),
                     'ts': int(msg.get('internalDate', 0)) / 1000})
    return sorted(mail, key=lambda m: m['ts'])[-8:]


def know(about: str) -> str:
    """Everything she has on a person, company, trip or topic: memory and past chats,
    mail both ways, calendar (past month and two ahead), commitments open and recently
    closed, and her notes on the person. Each source fails alone; an empty one says so,
    because "nothing in his mail" is itself an answer."""
    about = " ".join((about or "").split())
    terms = sorted(words(about), key=lambda w: (-len(w), w))[:5]
    if not terms:
        return "Give me a name, company, place or topic to look up."
    hit = lambda text: bool(words(text) & set(terms))   # noqa: E731
    sections = []

    def section(title, fn):
        try:
            body = fn()
        except Exception as e:
            print(f"[situation] know: {title} unavailable: {e}")
            body = None
            title += " (couldn't be read just now)"
        sections.append(f"{title}:\n{body}" if body else f"{title}: nothing found.")

    def from_memory():
        import memory
        return memory.recall(about, n_results=6, min_words=1)

    def from_mail():
        import email_backend
        from skills.email_manager import get_gmail_service
        service = None if email_backend.using_app_password() else get_gmail_service()
        if not service:
            raise RuntimeError("no Gmail service")
        return "\n".join(_mail_line(m) for m in _mail_about(service, about, terms))

    def from_calendar():
        from skills.google_calendar import fetch_events
        events = fetch_events(days=KNOW_AHEAD_DAYS, dated=True, back_days=KNOW_BACK_DAYS)
        if events is None:
            raise RuntimeError("no calendar service")
        return "\n".join(f"    {e}" for e in events if hit(e))

    def from_commitments():
        from skills.commitment_manager import (get_open_commitments, recently_closed,
                                               format_line)
        lines = [f"    open: {format_line(c)}" for c in get_open_commitments()
                 if hit(f"{c['description']} {c.get('who') or ''}")]
        lines += [f"    {c['status']} {str(c.get('closed_at') or '')[:10]}: {c['description']}"
                  for c in recently_closed(40) if hit(c['description'])][:6]
        return "\n".join(lines)

    def from_people():
        import people
        return "\n".join("    " + ", ".join(f"{k}: {v}" for k, v in p.items() if v)
                         for p in people._load()
                         if hit(" ".join([p.get('name', '')] + list(p.get('aliases', [])))))

    section("What you remember (memory and earlier chats; may include unrelated matches)",
            from_memory)
    section(f"Mail, last {KNOW_MAIL_DAYS} days, oldest first", from_mail)
    section(f"Calendar, past {KNOW_BACK_DAYS} days to {KNOW_AHEAD_DAYS} days ahead",
            from_calendar)
    section("Commitments", from_commitments)
    section("Your notes on people", from_people)
    return (f"Everything on {about!r}. These are parts of one picture: read them together, "
            f"and trust mail and calendar over an older memory.\n\n" + "\n\n".join(sections))
