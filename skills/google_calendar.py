"""Google Calendar connector — Aria's first external-app connector.

Satvik's standing instruction (2026-06-11): every personal event goes on BOTH
calendars — his personal one ('primary', where his default notifications live)
and the calendar shared with his girlfriend (so she sees it), color-coded
YELLOW (Google colorId 5, "Banana") on the shared one. His work calendar is
deliberately NOT connected.

That rule is enforced IN CODE (create_calendar_event always dual-writes), not
left to prompt memory — deterministic behaviors belong in tools, not vibes.

Auth: reuses the primary Google token (token.json); requires the calendar
scopes added 2026-06 (ADR 0006) — delete token.json and re-auth once after
pulling this change. The shared calendar's id lives in calendar_config.json
(gitignored), set conversationally: list_my_calendars -> configure_shared_calendar.
"""
import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlencode

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_core.tools import tool

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'calendar_config.json')
YELLOW = '5'  # Google Calendar colorId "Banana"


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)


def get_calendar_service():
    """Calendar API client on the primary Google token (same file as Gmail's)."""
    token_path = os.path.join(BASE_DIR, 'token.json')
    if not os.path.exists(token_path):
        return None
    try:
        creds = Credentials.from_authorized_user_file(token_path)
        if creds.expired and creds.refresh_token:
            import google.auth.transport.requests
            creds.refresh(google.auth.transport.requests.Request())
            # Persist the refreshed token (atomically) so we don't re-refresh every call.
            import token_store
            token_store.atomic_write_text(token_path, creds.to_json())
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Failed to build Calendar service: {e}")
        return None


def _event_body(title, date_iso, start_time=None, end_time=None, description=None):
    """Build the event payload; timed events carry the machine's local timezone."""
    body = {'summary': title}
    if description:
        body['description'] = description
    if start_time:
        start_dt = datetime.fromisoformat(f"{date_iso}T{start_time}:00")
        if end_time:
            end_dt = datetime.fromisoformat(f"{date_iso}T{end_time}:00")
        else:
            end_dt = start_dt + timedelta(hours=1)
        body['start'] = {'dateTime': start_dt.astimezone().isoformat()}
        body['end'] = {'dateTime': end_dt.astimezone().isoformat()}
    else:  # all-day
        next_day = (datetime.fromisoformat(date_iso) + timedelta(days=1)).strftime('%Y-%m-%d')
        body['start'] = {'date': date_iso}
        body['end'] = {'date': next_day}
    return body


def _gcal_link(title, date_iso, start_time=None, end_time=None, description=None,
               location=None) -> str:
    """A Google Calendar 'add event' template URL the user taps to add the event to
    THEIR OWN calendar — no OAuth, no token, lands in whatever account they're signed
    into. Used for guests (Path A): Aria never touches their calendar credentials.
    Naive times are interpreted in the user's own Google Calendar timezone."""
    if start_time:
        start_dt = datetime.fromisoformat(f"{date_iso}T{start_time}:00")
        end_dt = (datetime.fromisoformat(f"{date_iso}T{end_time}:00") if end_time
                  else start_dt + timedelta(hours=1))
        dates = f"{start_dt.strftime('%Y%m%dT%H%M%S')}/{end_dt.strftime('%Y%m%dT%H%M%S')}"
    else:  # all-day: end date is exclusive (next day)
        end = (datetime.fromisoformat(date_iso) + timedelta(days=1)).strftime('%Y%m%d')
        dates = f"{date_iso.replace('-', '')}/{end}"
    params = {'action': 'TEMPLATE', 'text': title, 'dates': dates}
    if description:
        params['details'] = description
    if location:
        params['location'] = location
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


# --- Agent tools ---

@tool
def add_to_calendar(title: str, date_iso: str, start_time: str = None,
                    end_time: str = None, description: str = None,
                    location: str = None) -> str:
    """Give the user a tap-to-add Google Calendar link for an event. Use this when they
    want something on their calendar (appointment, dinner, flight, meeting) — reminders
    and promises belong in add_commitment instead. The link adds the event to THEIR own
    calendar when they tap it; share the link back to them.

    Args:
        title: Event title (e.g. "Dinner with Priya").
        date_iso: Event date YYYY-MM-DD (compute from words like "Saturday").
        start_time: HH:MM 24h. Omit for an all-day event.
        end_time: HH:MM 24h. Defaults to one hour after start.
        description: Optional details.
        location: Optional location.
    """
    try:
        datetime.strptime(date_iso, '%Y-%m-%d')
        if start_time:
            datetime.strptime(start_time, '%H:%M')
        if end_time:
            datetime.strptime(end_time, '%H:%M')
    except ValueError as e:
        return f"Error: bad date/time format ({e}). Use YYYY-MM-DD and HH:MM."
    link = _gcal_link(title, date_iso, start_time, end_time, description, location)
    when = date_iso + (f" {start_time}" + (f"–{end_time}" if end_time else "")
                       if start_time else " (all day)")
    return f"Here's a link to add '{title}' ({when}) to your calendar — tap it:\n{link}"


@tool
def list_my_calendars() -> str:
    """List the user's Google calendars with their ids. Use during setup to find the
    calendar shared with his girlfriend, then save it with configure_shared_calendar."""
    service = get_calendar_service()
    if not service:
        return "Calendar isn't connected yet — the Google token needs the calendar scopes (re-auth required)."
    items = service.calendarList().list().execute().get('items', [])
    lines = [f"- {c.get('summary', '?')}  (id: {c['id']}, access: {c.get('accessRole', '?')})"
             for c in items]
    return "Your calendars:\n" + "\n".join(lines)


@tool
def configure_shared_calendar(calendar_id: str, label: str = "shared") -> str:
    """Save which calendar is the one shared with the user's girlfriend. From then on,
    create_calendar_event mirrors every event there (color-coded yellow)."""
    cfg = _load_config()
    cfg['shared_calendar_id'] = calendar_id
    cfg['shared_calendar_label'] = label
    _save_config(cfg)
    return f"Saved — events will now be mirrored to '{label}' ({calendar_id}) in yellow."


@tool
def create_calendar_event(title: str, date_iso: str, start_time: str = None,
                          end_time: str = None, description: str = None,
                          calendars: str = 'both') -> str:
    """Create an event on the user's calendar(s). Use this for appointments/events
    with a date — commitments/promises belong in add_commitment instead.

    Args:
        title: Event title (e.g. "Dinner with Priya").
        date_iso: Event date YYYY-MM-DD (compute from words like "Saturday").
        start_time: HH:MM 24h. Omit for an all-day event.
        end_time: HH:MM 24h. Defaults to one hour after start.
        description: Optional details/location.
        calendars: 'both' (DEFAULT — follow the user's standing instruction: personal
            + shared-in-yellow), 'personal_only', or 'shared_only'. Deviate from
            'both' ONLY when the user explicitly asks (e.g. "just my personal").
    """
    if calendars not in ('both', 'personal_only', 'shared_only'):
        return f"Error: calendars must be 'both', 'personal_only', or 'shared_only', got {calendars!r}."
    try:
        datetime.strptime(date_iso, '%Y-%m-%d')
        if start_time:
            datetime.strptime(start_time, '%H:%M')
        if end_time:
            datetime.strptime(end_time, '%H:%M')
    except ValueError as e:
        return f"Error: bad date/time format ({e}). Use YYYY-MM-DD and HH:MM."

    service = get_calendar_service()
    if not service:
        return "Calendar isn't connected yet — the Google token needs the calendar scopes (re-auth required)."

    body = _event_body(title, date_iso, start_time, end_time, description)
    cfg = _load_config()
    shared_id = cfg.get('shared_calendar_id')
    created, notes = [], []

    try:
        if calendars in ('both', 'personal_only'):
            service.events().insert(calendarId='primary', body=body).execute()
            created.append("personal")

        if calendars in ('both', 'shared_only') and shared_id:
            shared_body = dict(body, colorId=YELLOW)
            service.events().insert(calendarId=shared_id, body=shared_body).execute()
            created.append(f"{cfg.get('shared_calendar_label', 'shared')} (yellow)")
    except Exception as e:
        print(f"Calendar create failed: {e}")
        return "I couldn't add that to your calendar just now — want me to try again?"

    if calendars in ('both', 'shared_only') and not shared_id:
        if calendars == 'shared_only':
            return ("The shared calendar isn't configured yet — run calendar setup "
                    "(list_my_calendars, then configure_shared_calendar) first.")
        else:
            notes.append("shared calendar not configured yet — set it up so events reach it too")

    when = date_iso + (f" {start_time}" + (f"–{end_time}" if end_time else "") if start_time else " (all day)")
    msg = f"Created '{title}' ({when}) on: {', '.join(created)}."
    if notes:
        msg += f" (Note: {'; '.join(notes)}.)"
    return msg


def _search_events(service, query, date_iso=None, window_days=120):
    """Find events matching `query` across both calendars (NO stored IDs — the calendar
    is the source of truth, so we fetch at edit time; this finds events however they
    were created and never drifts). Returns [{calendar_id, event_id, summary, start}]."""
    if date_iso:
        day = datetime.fromisoformat(f"{date_iso}T00:00:00").astimezone()
        time_min, time_max = day.isoformat(), (day + timedelta(days=1)).isoformat()
    else:
        now = datetime.now().astimezone()
        time_min, time_max = now.isoformat(), (now + timedelta(days=window_days)).isoformat()

    cal_ids = ['primary']
    shared = _load_config().get('shared_calendar_id')
    if shared:
        cal_ids.append(shared)

    out = []
    for cal_id in cal_ids:
        try:
            items = service.events().list(
                calendarId=cal_id, q=query, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime').execute().get('items', [])
        except Exception as e:
            print(f"Calendar search failed for {cal_id}: {e}")
            continue
        for ev in items:
            start = ev.get('start', {})
            out.append({'calendar_id': cal_id, 'event_id': ev['id'],
                        'summary': ev.get('summary', ''),
                        'start': start.get('dateTime', start.get('date', ''))})
    return out


def _group_copies(matches):
    """Group matches by (summary, start) — dual-write copies of one logical event."""
    groups = {}
    for m in matches:
        groups.setdefault((m['summary'].lower(), m['start'][:16]), []).append(m)
    return groups


def _ambiguous_msg(groups):
    lines = [f"- {ms[0]['summary']} ({ms[0]['start']})" for ms in groups.values()]
    return "Several events match — which one?\n" + "\n".join(lines)


@tool
def update_calendar_event(query: str, date_iso: str = None, new_title: str = None,
                          new_date_iso: str = None, new_start_time: str = None,
                          new_end_time: str = None) -> str:
    """Edit an existing calendar event. Finds it by searching both calendars for `query`
    (optionally narrowed by date_iso), then applies the change to EVERY copy (personal +
    shared). Use to rename or reschedule. If several distinct events match, it lists them
    so you can be specific.

    Args:
        query: Text to find the event by (e.g. "dentist", "dinner with Priya").
        date_iso: YYYY-MM-DD to narrow the search to one day (optional).
        new_title: New event title.
        new_date_iso / new_start_time / new_end_time: New date/time (HH:MM 24h). Omit
            start_time to make it all-day on the new date.
    """
    service = get_calendar_service()
    if not service:
        return "Calendar isn't connected yet."
    matches = _search_events(service, query, date_iso)
    if not matches:
        return f"No event matching '{query}' found."
    groups = _group_copies(matches)
    if len(groups) > 1:
        return _ambiguous_msg(groups)

    copies = next(iter(groups.values()))
    body = {}
    if new_title:
        body['summary'] = new_title
    if new_date_iso or new_start_time:
        base_date = new_date_iso or copies[0]['start'][:10]
        eb = _event_body(new_title or copies[0]['summary'], base_date, new_start_time, new_end_time)
        # Explicitly null the counterpart key so a type change (all-day <-> timed) doesn't
        # leave the event with BOTH date and dateTime, which Google rejects with a 400.
        start, end = dict(eb['start']), dict(eb['end'])
        start['date' if 'dateTime' in start else 'dateTime'] = None
        end['date' if 'dateTime' in end else 'dateTime'] = None
        body['start'], body['end'] = start, end
    if not body:
        return "Nothing to change — give a new title and/or date/time."

    try:
        for c in copies:
            service.events().patch(calendarId=c['calendar_id'], eventId=c['event_id'], body=body).execute()
    except Exception as e:
        print(f"Calendar update failed: {e}")
        return "I couldn't update that event just now — want me to try again?"
    return f"Updated '{new_title or copies[0]['summary']}' on {len(copies)} calendar(s)."


@tool
def delete_calendar_event(query: str, date_iso: str = None) -> str:
    """Delete a calendar event from every calendar it's on. Finds it by searching both
    calendars for `query` (optionally narrowed by date_iso). If several distinct events
    match, it lists them so you can confirm which.

    Args:
        query: Text to find the event by.
        date_iso: YYYY-MM-DD to narrow to one day (optional).
    """
    service = get_calendar_service()
    if not service:
        return "Calendar isn't connected yet."
    matches = _search_events(service, query, date_iso)
    if not matches:
        return f"No event matching '{query}' found."
    groups = _group_copies(matches)
    if len(groups) > 1:
        return _ambiguous_msg(groups)

    copies = next(iter(groups.values()))
    summary = copies[0]['summary']
    try:
        for c in copies:
            service.events().delete(calendarId=c['calendar_id'], eventId=c['event_id']).execute()
    except Exception as e:
        print(f"Calendar delete failed: {e}")
        return "I couldn't remove that event just now — want me to try again?"
    return f"Deleted '{summary}' from {len(copies)} calendar(s)."


@tool
def get_calendar_events(days: int = 1, calendar: str = 'both') -> str:
    """The user's upcoming events. days=1 means today; 7 the next week; 90 for ~3 months.

    Args:
        days: How far ahead to look.
        calendar: 'both' (default — merged, dual-written copies collapsed, events only on
            the shared calendar tagged '[shared only]' — those are Ashley's/joint events),
            'personal' (his calendar only), or 'shared' (the shared calendar only — use
            when asked what's ON the shared calendar or about Ashley's events).
    """
    if calendar not in ('both', 'personal', 'shared'):
        return "Error: calendar must be 'both', 'personal', or 'shared'."
    events = fetch_events(days, calendar=calendar)
    if events is None:
        return "Calendar isn't connected yet — the Google token needs the calendar scopes (re-auth required)."
    if not events:
        return f"Nothing on the {calendar if calendar != 'both' else ''} calendar in that window.".replace("  ", " ")
    label = {"both": "Calendar", "personal": "Personal calendar", "shared": "Shared calendar"}[calendar]
    return f"{label}:\n" + "\n".join(f"- {e}" for e in events)


def fetch_events(days: int = 1, calendar: str = 'both'):
    """Event lines for tools and the briefing. Returns None when the service isn't there.

    calendar='both' (default): personal + shared merged, dual-written copies collapsed to
    one line, and events that exist ONLY on the shared calendar tagged '[shared only]' —
    per the user's rule those are Ashley's/joint events, and the tag preserves what the
    dedup used to erase. 'personal' / 'shared' scope to a single calendar (no dedup —
    what's on that calendar is exactly what you get)."""
    service = get_calendar_service()
    if not service:
        return None
    now = datetime.now().astimezone()
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).replace(hour=23, minute=59).isoformat()

    shared_id = _load_config().get('shared_calendar_id')
    sources = []
    if calendar in ('both', 'personal'):
        sources.append(('personal', 'primary'))
    if calendar in ('both', 'shared'):
        if shared_id:
            sources.append(('shared', shared_id))
        elif calendar == 'shared':
            return []          # shared not configured — nothing to show, not an error

    found = {}                 # key -> {"when","summary","start","cals":set}
    for cal_name, cal_id in sources:
        try:
            items = service.events().list(
                calendarId=cal_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime').execute().get('items', [])
        except Exception as e:
            print(f"Calendar fetch failed for {cal_id}: {e}")
            continue
        for ev in items:
            start = ev.get('start', {})
            when = start.get('dateTime', start.get('date', ''))
            key = (ev.get('summary'), when)   # dual-written events collapse to one entry
            entry = found.setdefault(key, {"when": when, "start": start,
                                           "summary": ev.get('summary', '(no title)'),
                                           "cals": set()})
            entry["cals"].add(cal_name)

    lines = []
    for e in sorted(found.values(), key=lambda x: x["when"]):   # chronological across cals
        if 'dateTime' in e["start"]:
            label = datetime.fromisoformat(e["when"]).strftime('%a %H:%M')
        else:
            label = datetime.fromisoformat(e["when"]).strftime('%a') + " (all day)"
        tag = " [shared only]" if (calendar == 'both' and e["cals"] == {'shared'}) else ""
        lines.append(f"{label} — {e['summary']}{tag}")
    return lines
