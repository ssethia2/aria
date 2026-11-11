"""What his mail and calendar already show about each open commitment.

A commitment row is one moving part of a task. The others live elsewhere: the reply he
sent yesterday, the invite that landed after it, the event now on his calendar. Judging a
row alone, Aria nudged him to "schedule the interview" that was already booked and to
answer a thread he had answered. around() gathers those parts for a set of commitments;
describe() renders them for a prompt or a tool result. enrich_mail() and in_motion() do the
same for anything judging incoming mail: each email gets its thread so far, what she
remembers about the sender and matter, and her notes on the person; the judge also gets his
calendar and open commitments. Read-only, owner-only, and it never raises: with no Gmail
or calendar it returns nothing and callers carry on as before.
"""
import re
import time
from datetime import datetime

WINDOW_DAYS = 21        # how far back mail still counts as part of the same matter
CALENDAR_DAYS = 30
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
        out["calendar"] = fetch_events(days=CALENDAR_DAYS, dated=True) or []
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
        blocks.append(f"On his calendar, next {CALENDAR_DAYS} days:\n"
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
