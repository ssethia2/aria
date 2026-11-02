"""Moments — the anticipation layer of Aria's proactivity engine.

The engine's monitors react to what already happened: new mail, a commitment coming
due. Moments look AHEAD. Each timed thing on his calendar (a meeting, a flight, a
reservation) is a moment with touchpoints, the times worth speaking relative to it:
an hour before a meeting, the day before a flight, the morning after an interview.

At a touchpoint the engine does the legwork BEFORE saying anything (the pattern Muse
and Instinct share: come back with the work done, not a question). It reads the email
thread with the people involved, finds open commitments to them, and puts the time in
his timezone. Then a judge (engine.MomentMonitor) decides whether any of that adds
something beyond Google's own reminder, and writes at most one message.

Adding a kind of moment means adding a classifier branch and a TOUCHPOINTS entry; the
prep, judgment, quiet-hours timing and the engine's shared gate come with it.

Reads his personal calendar only: events that exist only on the shared calendar are
his girlfriend's (standing rule), not his.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

QUIET_HOURS = (23, 8)   # the engine's default; touchpoints are timed to land outside it
PROFILE_PATH = os.path.join(os.path.dirname(__file__), "profile.json")


@dataclass(frozen=True)
class Touchpoint:
    name: str              # 'before' | 'day_before' | 'after'
    anchor: str            # 'start' | 'end'
    offset: timedelta      # from the anchor (negative = before it)
    min_lead: timedelta    # a 'before' heads-up later than this is pointless; skip it


TOUCHPOINTS = {
    # An hour gives time to act on what the thread says (prep, a link, a document);
    # the morning after is when a thank-you note or a promised follow-up is due.
    "meeting": (Touchpoint("before", "start", -timedelta(minutes=60), timedelta(minutes=10)),
                Touchpoint("after", "end", timedelta(hours=2), timedelta(0))),
    # The day before is when check-in opens and an early start needs planning.
    "flight": (Touchpoint("day_before", "start", -timedelta(hours=24), timedelta(hours=6)),),
    "booking": (Touchpoint("before", "start", -timedelta(hours=3), timedelta(minutes=45)),),
}
AFTER_WINDOW = timedelta(hours=18)   # an 'after' touchpoint stays live this long past the end


@dataclass
class Moment:
    key: str                         # calendar id + start: a moved event fires afresh
    kind: str                        # 'meeting' | 'flight' | 'booking'
    title: str
    start: datetime                  # timezone-aware
    end: datetime
    event_tz: str = None             # the invite's own timezone, e.g. 'America/New_York'
    people: list = field(default_factory=list)   # [{"name", "email"}], him excluded
    location: str = ""
    link: str = ""                   # the video-call link Google attached, if any
    description: str = ""


# ---- reading the calendar ----------------------------------------------------------

_FLIGHT_RE = re.compile(r'\(([A-Z0-9]{2})\s?(\d{1,4})\)')   # "Flight to Lisbon (TP 202)"
_GENERIC = {
    "interview", "interviews", "with", "and", "the", "for", "call", "meeting", "meet",
    "sync", "chat", "catch", "intro", "zoom", "google", "teams", "invitation", "invite",
    "reservation", "flight", "to", "at", "of", "a", "an", "on", "in", "x", "re",
    "round", "screen", "phone", "video", "recruiting", "recruiter", "hiring", "appt",
    "appointment", "follow", "up", "followup", "discussion", "session", "1:1", "update",
    "new",
}


_identity = {}


def my_name() -> str:
    """His name and address (profile.json + his primary calendar's id), so that 'Sam Lee
    and Acme' reduces to 'Acme' — and his surname, which the profile may not carry, doesn't
    count as a distinctive word that matches every commitment with his name in it."""
    if "me" in _identity:
        return _identity["me"]
    try:
        with open(PROFILE_PATH) as f:
            name = json.load(f).get("name") or ""
    except Exception:
        name = ""
    try:
        from skills.google_calendar import get_calendar_service
        address = get_calendar_service().calendarList().get(
            calendarId="primary").execute().get("id", "")
    except Exception:
        return name                  # not cached: try for the address again next time
    _identity["me"] = f"{name} {address}".strip()
    return _identity["me"]


def _aware(when: str) -> datetime:
    return datetime.fromisoformat(when.replace("Z", "+00:00"))


def _kind(ev: dict) -> str:
    title = ev.get("summary") or ""
    if ev.get("eventType") == "fromGmail":
        # Gmail adds flights and reservations it finds in mail to the calendar.
        return "flight" if title.lower().startswith("flight") else "booking"
    return "meeting"


def _people(ev: dict) -> list:
    out, seen = [], set()
    for a in ev.get("attendees", []) + [ev.get("organizer") or {}]:
        email = (a.get("email") or "").lower()
        if (not email or a.get("self") or a.get("resource") or email in seen
                or email.endswith("calendar.google.com")):   # group/room calendars
            continue
        seen.add(email)
        out.append({"name": a.get("displayName") or email.split("@")[0], "email": email})
    return out


def _video_link(ev: dict) -> str:
    for ep in (ev.get("conferenceData") or {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    return ev.get("hangoutLink") or ""


def _declined(ev: dict) -> bool:
    return any(a.get("self") and a.get("responseStatus") == "declined"
               for a in ev.get("attendees", []))


def distinctive_words(text: str, me: str = "") -> list:
    """Title words that identify the thing ('Acme' in 'Interview with Acme'), minus
    generic meeting words and his own name."""
    mine = {w.lower() for w in re.findall(r"[A-Za-z]+", me)}

    def is_mine(w):   # 'mercer' is inside the address 'sammercer@…'
        return w in mine or (len(w) >= 4 and any(w in m for m in mine))

    words = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9&'.-]*", text):
        lw = w.lower().strip(".-'")
        if len(lw) >= 3 and lw not in _GENERIC and not is_mine(lw) and lw not in words:
            words.append(lw)
    return words


def from_events(items: list, me: str = "") -> list:
    """Calendar API events → moments. Skips all-day events (birthdays and the like are
    the morning briefing's job), cancelled ones, and ones he declined."""
    moments = []
    for ev in items:
        start, end = ev.get("start", {}), ev.get("end", {})
        if "dateTime" not in start or ev.get("status") == "cancelled" or _declined(ev):
            continue
        s = _aware(start["dateTime"])
        e = _aware(end["dateTime"]) if "dateTime" in end else s + timedelta(hours=1)
        moments.append(Moment(
            key=f"{ev.get('id', '')}@{s.isoformat()}", kind=_kind(ev),
            title=(ev.get("summary") or "(untitled)").strip(), start=s, end=e,
            event_tz=start.get("timeZone"), people=_people(ev),
            location=ev.get("location") or "", link=_video_link(ev),
            description=re.sub(r"<[^>]+>", " ", ev.get("description") or "")[:1500]))
    return _merge_duplicates(moments, me)


def _merge_duplicates(moments: list, me: str) -> list:
    """One meeting often appears twice: the company's scheduling calendar sends one
    invite and the interviewer another, same time, overlapping words or people."""
    merged = []
    for m in sorted(moments, key=lambda x: x.start):
        twin = next((o for o in merged if o.kind == m.kind == "meeting" and o.start == m.start
                     and (set(distinctive_words(o.title, me)) & set(distinctive_words(m.title, me))
                          or {p["email"] for p in o.people} & {p["email"] for p in m.people})),
                    None)
        if not twin:
            merged.append(m)
            continue
        twin.title = f"{twin.title} / {m.title}"
        known = {p["email"] for p in twin.people}
        twin.people += [p for p in m.people if p["email"] not in known]
        twin.location = twin.location or m.location
        twin.link = twin.link or m.link
        twin.description = (twin.description + "\n" + m.description).strip()[:2000]
    return merged


def upcoming(now: datetime, me: str = "") -> list:
    """Moments from his personal calendar that could have a touchpoint around now:
    from a day back (the morning after) to three days out (the day before a flight)."""
    from skills.google_calendar import get_calendar_service
    service = get_calendar_service()
    if not service:
        return []
    items = service.events().list(
        calendarId="primary", singleEvents=True, orderBy="startTime",
        timeMin=(now - timedelta(days=1)).isoformat(),
        timeMax=(now + timedelta(days=3)).isoformat()).execute().get("items", [])
    return from_events(items, me)


# ---- timing --------------------------------------------------------------------------

def _local(now: datetime) -> datetime:
    """`now` in his timezone: as given when it carries one, else the host's."""
    return now if now.tzinfo else now.astimezone()


def _quiet(t: datetime, quiet) -> bool:
    start, end = quiet
    return (t.hour >= start or t.hour < end) if start > end else (start <= t.hour < end)


def _shift_out_of_quiet(t: datetime, deadline: datetime, min_lead: timedelta, quiet):
    """A touchpoint that lands in quiet hours moves to the next morning if that still
    leaves enough lead time, else to the evening before quiet hours start."""
    if not _quiet(t, quiet):
        return t
    start, end = quiet
    morning = t.replace(hour=end, minute=30, second=0, microsecond=0)
    if t.hour >= start:
        morning += timedelta(days=1)
    if morning <= deadline - min_lead:
        return morning
    evening = t.replace(hour=start, minute=0, second=0, microsecond=0) - timedelta(minutes=30)
    if t.hour < end:
        evening -= timedelta(days=1)
    return evening


def due_touchpoints(m: Moment, now: datetime, quiet=QUIET_HOURS) -> list:
    """The touchpoints of `m` whose time has come and not yet passed. All times are
    compared in his local timezone (the host's), whatever zone the invite was set in."""
    local = _local(now)
    start, end = m.start.astimezone(local.tzinfo), m.end.astimezone(local.tzinfo)
    due = []
    for tp in TOUCHPOINTS.get(m.kind, ()):
        at = (start if tp.anchor == "start" else end) + tp.offset
        deadline = start - tp.min_lead if tp.anchor == "start" else end + AFTER_WINDOW
        at = _shift_out_of_quiet(at, deadline, tp.min_lead, quiet)
        if at <= local < deadline:
            due.append(tp)
    return due


def when_text(m: Moment, now: datetime) -> str:
    """'Tue 3 Nov, 10:00 PM your time (11:30 AM in New York), in 2h' — an invite set in
    another timezone is where a missed interview comes from."""
    local = _local(now)
    s = m.start.astimezone(local.tzinfo)
    text = f"{s.strftime('%a %-d %b, %-I:%M %p')} your time"
    if m.event_tz:
        try:
            theirs = m.start.astimezone(ZoneInfo(m.event_tz))
            if theirs.utcoffset() != s.utcoffset():
                city = m.event_tz.split("/")[-1].replace("_", " ")
                text += f" ({theirs.strftime('%-I:%M %p')} in {city})"
        except Exception:
            pass
    mins = int((s - local).total_seconds() // 60)
    if mins >= 0:
        text += f", starts in {mins // 60}h {mins % 60:02d}m" if mins >= 60 else f", starts in {mins}m"
    else:
        text += f", ended {int((local - m.end.astimezone(local.tzinfo)).total_seconds() // 3600)}h ago"
    return text


# ---- the legwork ---------------------------------------------------------------------

def search_query(m: Moment, me: str = "") -> str:
    """The Gmail search that finds this moment's thread."""
    if m.kind == "flight":
        codes = [f'"{a} {n}" OR "{a}{n}"' for a, n in _FLIGHT_RE.findall(m.title)]
        if codes:
            return " OR ".join(codes)
    if m.kind == "booking":
        place = re.sub(r"(?i)^reservation (at|for)\s+", "", m.title).split(" - ")[0].strip()
        return f'"{place}"'
    if m.people:
        return " OR ".join(f"from:{p['email']} OR to:{p['email']}" for p in m.people[:3])
    words = distinctive_words(m.title, me)
    return " ".join(words[:2])


def _name_words(m: Moment, me: str) -> list:
    words = distinctive_words(m.title, me)
    for p in m.people:
        words += [w for w in distinctive_words(p["name"], me) if w not in words]
    return words


def gather(m: Moment, me: str = "") -> dict:
    """What a good assistant would look up before speaking. Each source is optional; a
    failure in one never blocks the others."""
    found = {"email": "", "commitments": [], "people": []}
    query = search_query(m, me)
    if query:
        # Calendar's own invite/update/cancel mails (they carry an .ics) only repeat the
        # event — and a cancelled version's Zoom link, read as "the latest", once nearly
        # sent him to a dead meeting an hour before an interview.
        if m.kind == "meeting":
            query = f"({query}) -filename:ics"
        try:
            from skills.email_manager import read_email_thread
            text = read_email_thread.invoke({"search_query": f"({query}) newer_than:120d"})
            if "found no matching" not in text and "isn't available" not in text:
                found["email"] = text[:4000]
        except Exception as e:
            print(f"[moments] email lookup failed: {e}")
    words = _name_words(m, me)
    try:
        from skills.commitment_manager import get_open_commitments
        found["commitments"] = [
            f"#{c['id']} {c['description']}" + (f" (due {c['due_date']})" if c.get("due_date") else "")
            for c in get_open_commitments()
            if any(re.search(rf"\b{re.escape(w)}\b", c["description"].lower()) for w in words)]
    except Exception as e:
        print(f"[moments] commitments lookup failed: {e}")
    try:
        import people
        roster = people._load()
        for p in m.people:
            hit = people._find(roster, p["name"])   # full name only: first names collide
            if hit:
                found["people"].append(", ".join(f"{k}: {v}" for k, v in hit.items() if v))
    except Exception as e:
        print(f"[moments] people lookup failed: {e}")
    return found


def has_substance(m: Moment, found: dict) -> bool:
    """Is there anything to add beyond Google's own reminder? With no thread, no
    commitment, no person and no one else invited, the judge isn't worth a call."""
    return bool(found["email"] or found["commitments"] or found["people"] or m.people
                or (m.kind == "meeting" and m.description.strip()))


_CALL_ID = re.compile(r"zoom\.us/j/(\d+)|meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", re.I)


def unknown_call_links(message: str, m: Moment, found: dict) -> list:
    """Join links in `message` that appear neither on the event nor in the mail we read —
    a link the model got wrong or made up. Wrong is worse than silent here."""
    known = " ".join([m.location, m.link, m.description, found.get("email", "")]).lower()
    ids = ["".join(g).lower() for g in _CALL_ID.findall(message)]
    return [i for i in ids if i not in known]


def describe(m: Moment, now: datetime) -> str:
    lines = [f"{m.kind}: {m.title}", f"when: {when_text(m, now)}"]
    if m.people:
        lines.append("with: " + ", ".join(f"{p['name']} <{p['email']}>" for p in m.people))
    if m.location:
        lines.append(f"where: {m.location}")
    if m.link:
        lines.append(f"video link: {m.link}")
    if m.description.strip():
        lines.append("invite notes: " + " ".join(m.description.split())[:800])
    return "\n".join(lines)
