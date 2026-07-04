"""Aria's proactivity engine — she notices things and reaches out unprompted.

A background loop of pluggable Monitors, each polled on its own interval:
  - CommitmentMonitor:  pings timed commitments at their moment (date-only ones
                        surface in the briefing/digest instead — no random pings)
  - EmailDigestMonitor: LLM-screens new mail; flagged items accumulate into ONE
                        evening digest, and replies-owed become tracked commitments
  - ChaseMonitor:       daytime judgment nudges on aging/overdue commitments
  - InsightMonitor:     twice-daily cross-source intelligence — looks at calendar +
                        commitments + weather together and surfaces one non-obvious,
                        useful thing (or stays silent, the default)
  - NetflixMonitor:     spots a fresh "Update Netflix Household" email, runs the
                        browser automation immediately, reports the outcome

Quiet hours (23:00–08:00 by default): monitors still RUN and act, but notifications
queue and flush as one "while you were away" digest in the morning. Notifications
marked urgent bypass quiet hours. State (seen ids, queued pings) persists in
engine_state.json so restarts don't re-notify or drop the queue.

Runs as a daemon thread inside telegram_bot.py (start_engine_thread). Standalone:
`python3 engine.py --once` for a single tick, `python3 engine.py` to loop forever.
Set ARIA_ENGINE_DISABLED=1 to keep the bot from starting it.
"""
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from dotenv import load_dotenv

from notify import send_telegram

load_dotenv()

STATE_PATH = os.path.join(os.path.dirname(__file__), "engine_state.json")

_llm = None


def _get_llm():
    """Lazy, cached router LLM — light tier: screening runs every 15 min, cost matters."""
    global _llm
    if _llm is None:
        from llm_router import get_llm
        _llm = get_llm(temperature=0, tier="light")
    return _llm


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"[engine] ⚠️ corrupt state file, starting fresh: {e}")
        return {}


def save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


@dataclass
class Notification:
    text: str
    urgent: bool = False  # urgent notifications bypass quiet hours


def _parse_llm_json(content: str):
    """Extract the first JSON array/object from an LLM reply.

    Light-tier models wrap output in ```json fences and add commentary despite
    instructions (seen live 2026-06-10); a plain json.loads chokes on that.
    Raises json.JSONDecodeError if no JSON is found.
    """
    match = re.search(r'\[.*\]|\{.*\}', content, re.DOTALL)
    if not match:
        raise json.JSONDecodeError("no JSON block found", content, 0)
    return json.loads(match.group(0))


def _record_to_memory(text: str):
    """Mirror an engine notification into Aria's working memory (Tier 1 scratchpad).

    Engine actions bypass the agent loop, so without this chat-Aria has no idea what
    her proactive side did and will deny it when asked — the split-brain problem.
    """
    try:
        import memory
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        line = f"{memory.ACTION_LOG_PREFIX}, {stamp}] " + " ".join(text.split())
        with open(memory.SCRATCHPAD_PATH, "a") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[engine] ⚠️ couldn't record action to memory: {e}")


@dataclass
class Monitor:
    """Base class: subclasses set name/interval and implement check(state).

    check() may mutate `state` (its slice of engine_state.json) and returns a list
    of Notification. It must not raise for routine failures — but the engine guards
    every call anyway, so one broken monitor never takes down the rest.
    """
    name: str = "monitor"
    interval_seconds: int = 300
    _next_due: float = field(default=0.0, repr=False)

    def check(self, state: dict) -> list:
        raise NotImplementedError


class CommitmentMonitor(Monitor):
    """Pings timed commitments at their moment (within ~2 min).

    Date-only commitments deliberately do NOT ping — a random 8 AM buzz for "sometime
    today" erodes trust. They surface in the morning briefing and evening digest.
    """

    def __init__(self, interval_seconds=120, now_fn=None):
        super().__init__(name="commitments", interval_seconds=interval_seconds)
        self.now_fn = now_fn or datetime.now

    def check(self, state: dict) -> list:
        from skills.commitment_manager import get_pingable_now, advance_recurring

        now = self.now_fn()
        today = now.strftime('%Y-%m-%d')
        notified = set(state.get("notified", {}).get(today, []))

        out = []
        for c in get_pingable_now(now):
            recurring = c.get('is_recurring')
            if c["id"] in notified and not recurring:
                continue
            who = f" ({c['who']})" if c.get('who') else ""
            when = f" — scheduled for {c['due_time']}" if c.get('due_time') else ""
            tag = f" (repeats {c['recurring'].replace('_', ' ')})" if recurring else ""
            # A reminder with an explicit due_time is an alarm the user SET for that exact
            # moment — honor it even in quiet hours (urgent). A 3 AM "leave for airport" ping
            # queued until 08:00 is a missed reminder. Date-only/recurring-without-time items
            # fire mid-morning (outside quiet hours) and stay non-urgent.
            out.append(Notification(f"⏰ {c['description']}{who}{when}{tag}",
                                    urgent=bool(c.get('due_time'))))
            if recurring:
                advance_recurring(c["id"], today)  # roll to next occurrence
            else:
                notified.add(c["id"])

        state["notified"] = {today: sorted(notified)}
        return out


class EmailDigestMonitor(Monitor):
    """Screens new inbox mail but NEVER pings instantly: flagged items accumulate and
    go out as ONE evening digest (~18:00). Emails that need a reply also become
    reply_owed commitments, so the chase loop owns them.

    (Design per the 2026-06 needs interview: <5 decision-emails/day — instant pings
    were noise; a digest plus tracked replies-owed is the right weight.)
    """

    SCREEN_PROMPT = """You are screening a user's incoming email for a twice-daily digest.

FLAG (worth surfacing) anything time-sensitive, personally significant, or actionable:
a real correspondent, bills due, security alerts, travel/booking changes, or a reply
that may be owed on the user's OWN booking/order/thread (e.g. an Airbnb host or a vendor
following up on something the user started). When in doubt, flag it — surfacing is cheap.
Do NOT flag newsletters, promotions, receipts, or social notifications.

needs_reply = TRUE only when the user genuinely OWES a reply to keep something of THEIRS
moving — a person or counterparty (friend, colleague, a host/vendor on the user's own
booking or order) is waiting on the user's answer, confirmation, or decision.
needs_reply = FALSE for solicitations the user can simply ignore — cold outreach,
recruiting, focus-group / survey / study invitations, sales, "would you be interested",
events the user didn't initiate — EVEN when phrased as a question from a real person.
needs_reply = FALSE for ALL automated notices — bank/card alerts and rate-change notices,
booking/order confirmations, email-validation or verify-your-account messages, shipping
updates, platform notifications. A machine sent them; no human is waiting on a reply.
If you're unsure, set needs_reply = false but still flag it with a reason noting it MIGHT
warrant a reply, so the user can judge.

Emails (JSON):
{emails}

Return ONLY a raw JSON array (no markdown), one object per flagged email:
[{{"id": "<email id>", "reason": "<one short sentence; say if it may need a reply>", "needs_reply": true/false}}]
Return [] if nothing qualifies — that should be the common case."""

    DIGEST_HOUR = 18  # evening flush; the 08:00 briefing covers the morning side

    def __init__(self, interval_seconds=900, now_fn=None):
        super().__init__(name="email-digest", interval_seconds=interval_seconds)
        self.now_fn = now_fn or datetime.now

    def check(self, state: dict) -> list:
        self._screen_new_mail(state)
        return self._maybe_flush_digest(state)

    def _screen_new_mail(self, state: dict):
        import email_backend
        from skills.email_manager import get_gmail_service

        now_ts = time.time()
        last_ts = state.get("last_check_ts", now_ts - self.interval_seconds)
        seen = state.get("seen_ids", [])

        if email_backend.using_app_password():
            fetched = email_backend.imap_fetch_since(last_ts - 120)
        else:
            service = get_gmail_service()
            if not service:
                print("[engine] email-digest: no Gmail service, skipping tick")
                return
            query = f"in:inbox after:{int(last_ts - 120)}"  # 2-min overlap; dedupe handles repeats
            resp = service.users().messages().list(
                userId='me', q=query, maxResults=20).execute()
            fetched = []
            for m in resp.get('messages', []):
                msg = service.users().messages().get(userId='me', id=m['id']).execute()
                headers = msg['payload'].get('headers', [])
                fetched.append({
                    'id': m['id'],
                    'subject': next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject'),
                    'sender': next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown'),
                    'snippet': msg.get('snippet', ''),
                    'list_unsubscribe': next(
                        (h['value'] for h in headers if h['name'].lower() == 'list-unsubscribe'), ''),
                })

        new = [e for e in fetched if e['id'] not in seen]
        state["last_check_ts"] = now_ts
        state["seen_ids"] = (seen + [e['id'] for e in new])[-300:]
        if not new:
            return

        # Deterministic pre-filter: drop bulk mail before spending an LLM call on it.
        from email_filter import is_bulk
        emails = [e for e in new if not is_bulk(e)]
        dropped = len(new) - len(emails)
        if dropped:
            print(f"[engine] email-digest: skipped {dropped} bulk email(s) before LLM screening")
        if not emails:
            return

        print(f"[engine] email-digest: screening {len(emails)} new email(s)")
        response = _get_llm().invoke(
            self.SCREEN_PROMPT.format(emails=json.dumps(emails, indent=2)))
        content = response.content
        if isinstance(content, list):
            content = next((b['text'] for b in content
                            if isinstance(b, dict) and b.get('type') == 'text'), str(content))
        try:
            flagged = _parse_llm_json(content)
        except json.JSONDecodeError:
            print(f"[engine] email-digest: unparseable screen result: {content[:200]}")
            return

        from skills import commitment_manager
        by_id = {e['id']: e for e in emails}
        converted = state.get("reply_commitment_ids", [])
        for item in flagged:
            email = by_id.get(item.get('id'))
            if not email:
                continue
            entry = {"sender": email['sender'], "subject": email['subject'],
                     "reason": item.get('reason', '')}
            # Deterministic guard: you can't OWE a reply to a no-reply/automated sender
            # (bank notices, booking confirmations, validation emails) no matter what the
            # screening LLM said. Still flagged in the digest — just never tracked.
            from email_filter import is_noreply_sender
            if item.get('needs_reply') and is_noreply_sender(email['sender']):
                item['needs_reply'] = False
            if item.get('needs_reply') and email['id'] not in converted:
                sender_name = email['sender'].split('<')[0].strip(' "') or email['sender']
                # A thread RE-OWES a reply each time they write back. So: if there's an
                # open reply-owed for this thread, only treat this inbound as new if the
                # user already replied to the prior one (then close it). If they haven't
                # replied yet, it's the same outstanding reply — don't double-track.
                existing = commitment_manager.open_reply_owed_for(sender_name, email['subject'])
                create = True
                if existing:
                    from skills.email_manager import user_has_replied
                    prior_subject = existing['description'].split(': ', 1)[-1]
                    if user_has_replied(prior_subject, existing.get('created_at')):
                        commitment_manager.complete(existing['id'])  # prior reply done; this is fresh
                    else:
                        create = False
                if create:
                    # NO fabricated due date. The sender set no deadline; inventing "+2 days"
                    # made Aria present made-up dates as real ("I was making up due dates").
                    # The chase loop nudges reply_owed by AGE, and the briefing shows open ones.
                    cid = commitment_manager.add(
                        description=f"Reply to {sender_name}: {email['subject']}",
                        kind='reply_owed', who=sender_name, source='email')
                    entry["tracked"] = f"#{cid}"
                else:
                    entry["tracked"] = "(you already owe a reply on this thread)"
                converted.append(email['id'])
            state.setdefault("pending_digest", []).append(entry)
        state["reply_commitment_ids"] = converted[-300:]

    def _maybe_flush_digest(self, state: dict) -> list:
        now = self.now_fn()
        today = now.strftime('%Y-%m-%d')
        if now.hour < self.DIGEST_HOUR or state.get("digest_date") == today:
            return []
        state["digest_date"] = today
        items = state.get("pending_digest", [])
        if not items:
            return []
        state["pending_digest"] = []
        lines = []
        tracked = 0
        for it in items:
            line = f"• {it['sender']} — “{it['subject']}”: {it['reason']}"
            if it.get("tracked"):
                line += f" (tracking reply as {it['tracked']})"
                tracked += 1
            lines.append(line)
        header = "📬 Evening inbox digest:"
        footer = (f"\n\nI'm tracking {tracked} reply(ies) you owe — "
                  "they'll be in tomorrow's briefing too.") if tracked else ""
        return [Notification(header + "\n" + "\n".join(lines) + footer)]


class ChaseMonitor(Monitor):
    """The chase loop: judgment-based nudges so open commitments don't quietly age out.

    A few times a day (daytime only), the light-tier LLM reviews open commitments and
    decides whether ONE short nudge is worth sending — overdue items, things due soon,
    aging replies, stale undated promises. Conservative by design: silence is the
    default, at most one nudge per day, and a commitment isn't re-nudged for days.
    """

    PROMPT = """You are Aria's judgment for whether to proactively nudge her user about open
commitments. Default to SILENCE — most checks should send nothing. A nudge is warranted
only for: something OVERDUE, something due today or tomorrow where lead time helps, a
reply owed that is several days old, or an undated commitment open for 7+ days (gently
ask whether it's still happening). Never re-nudge a commitment whose last_nudged is
within the past 3 days.

If a nudge IS warranted, write ONE short, warm message as Aria in first person — natural
and specific, zero nagging tone. Fold multiple items into that one message.

Today: {today}
Open commitments (JSON):
{commitments}

Return ONLY raw JSON (no markdown):
{{"send": true/false, "message": "<the message, or empty>", "commitment_ids": [<ids referenced>]}}"""

    def __init__(self, interval_seconds=10800, now_fn=None):
        super().__init__(name="chase", interval_seconds=interval_seconds)
        self.now_fn = now_fn or datetime.now

    def check(self, state: dict) -> list:
        now = self.now_fn()
        if not (10 <= now.hour < 21):
            return []  # daytime judgment only; mornings belong to the briefing
        today = now.strftime('%Y-%m-%d')
        if state.get("nudge_sent_date") == today:
            return []  # at most one nudge per day

        from skills.commitment_manager import get_open_commitments
        open_ = get_open_commitments()
        if not open_:
            return []

        nudged = state.get("nudged", {})
        for c in open_:
            c["last_nudged"] = nudged.get(str(c["id"]))

        response = _get_llm().invoke(
            self.PROMPT.format(today=today, commitments=json.dumps(open_, indent=2)))
        content = response.content
        if isinstance(content, list):
            content = next((b['text'] for b in content
                            if isinstance(b, dict) and b.get('type') == 'text'), str(content))
        try:
            verdict = _parse_llm_json(content)
        except json.JSONDecodeError:
            print(f"[engine] chase: unparseable verdict: {content[:200]}")
            return []

        if not verdict.get("send") or not verdict.get("message"):
            return []

        for cid in verdict.get("commitment_ids", []):
            nudged[str(cid)] = today
        state["nudged"] = nudged
        state["nudge_sent_date"] = today
        return [Notification(verdict["message"])]


class ReplyResolveMonitor(Monitor):
    """Auto-close reply-owed commitments the user has actually answered. A few times a
    day it checks each open reply_owed against sent mail and completes the ones replied
    to — so a thread you've handled stops nagging (the #13 case). Gmail-API only."""

    def __init__(self, interval_seconds=14400):  # every 4h
        super().__init__(name="reply-resolve", interval_seconds=interval_seconds)

    def check(self, state: dict) -> list:
        from skills.commitment_manager import resolve_replied
        from skills.email_manager import user_has_replied
        resolved = resolve_replied(user_has_replied)
        if not resolved:
            return []
        return [Notification("✅ Looks like you've replied to: "
                             + "; ".join(resolved) + " — marked done.")]


class HeartbeatMonitor(Monitor):
    """External dead-man's-switch: pings HEARTBEAT_URL every ~15 min so an outside
    monitor (healthchecks.io) can alert if the whole host goes silent — the one
    failure Aria can't report herself (a dead process can't send Telegram). Pings
    /fail when a quick local check is unhealthy, so the monitor sees health too.
    Returns no notifications; it's a pure side-effect ping.
    """

    def __init__(self, interval_seconds=900):
        super().__init__(name="heartbeat", interval_seconds=interval_seconds)

    def check(self, state: dict) -> list:
        from heartbeat import configured, send_heartbeat
        if not configured():
            return []
        # Cheap local liveness signal — avoid the full networked healthcheck every
        # 15 min; HealthMonitor owns the deep check. Engine ticking == core alive.
        send_heartbeat(healthy=True, note=f"aria {datetime.now().isoformat(timespec='minutes')}")
        return []


class HealthMonitor(Monitor):
    """Proactive watchdog: runs the self-diagnosis every few hours and alerts the
    user when something turns FAIL — so a silent breakage (dead token, stalled
    engine, missed briefing) becomes a message instead of weeks of nothing.
    Dedups: one alert per distinct failure-set per day.
    """

    def __init__(self, interval_seconds=10800):
        super().__init__(name="health", interval_seconds=interval_seconds)

    def check(self, state: dict) -> list:
        from healthcheck import run_all, summary, FAIL

        results = run_all()
        fails = sorted(name for name, s, _ in results if s == FAIL)
        today = datetime.now().strftime('%Y-%m-%d')
        if not fails:
            state.pop('alerted_sig', None)
            return []
        sig = ";".join(fails)
        if state.get('alerted_sig') == sig and state.get('alerted_date') == today:
            return []  # already flagged this exact problem today
        state['alerted_sig'] = sig
        state['alerted_date'] = today
        return [Notification("🩺 Something needs attention:\n\n" + summary(results))]


class InsightMonitor(Monitor):
    """Proactive intelligence — the 'is there anything actually worth telling Satvik?'
    pass. Twice a day it looks ACROSS calendar + commitments + weather together and
    surfaces ONE non-obvious, useful thing a sharp human assistant would mention — a
    conflict, a good window to clear an aging task, weather that breaks a plan, a
    connection he'd miss. Silence is the default; it never restates what he can already
    see, and never repeats a recent insight. This is judgment, not a data dump (that's
    the morning briefing's job).
    """

    PROMPT = """You are Aria's proactive intelligence. Looking across the user's day, decide
if there is ONE genuinely useful thing worth telling him RIGHT NOW, unprompted — the kind
of remark a sharp human chief-of-staff makes. Default HARD to saying nothing.

WORTH surfacing: a scheduling conflict or tight turnaround; a good open window to knock out
an overdue or aging task; weather that affects a specific plan; a connection between items
he might miss; a heads-up that gives useful lead time.
NOT worth surfacing: restating his calendar or commitments (he can see those); generic
reminders; anything obvious; anything similar to what you've recently told him.

You have recently told him (do NOT repeat these or close variants):
{recent}

It is {when}. Here is his current context:
{context}

Return ONLY JSON (no markdown):
{{"send": true/false, "insight": "<one or two warm, specific sentences, or empty>"}}"""

    def __init__(self, interval_seconds=7200, now_fn=None):
        super().__init__(name="insight", interval_seconds=interval_seconds)
        self.now_fn = now_fn or datetime.now

    def _gather(self):
        parts = []
        try:
            from skills.commitment_manager import (get_due_commitments,
                                                   get_upcoming_commitments, format_line)
            items = get_due_commitments() + get_upcoming_commitments(days=3)
            if items:
                parts.append("OPEN COMMITMENTS:\n" + "\n".join(format_line(c) for c in items))
        except Exception as e:
            print(f"[insight] commitments gather failed: {e}")
        try:
            from skills.google_calendar import fetch_events
            ev = fetch_events(days=2)
            if ev:
                parts.append("CALENDAR (next 2 days):\n" + "\n".join(ev))
        except Exception as e:
            print(f"[insight] calendar gather failed: {e}")
        try:
            from skills.weather_manager import fetch_weather_lines
            w = fetch_weather_lines(days=2)
            if w:
                parts.append("WEATHER:\n" + "\n".join(w))
        except Exception as e:
            print(f"[insight] weather gather failed: {e}")
        return "\n\n".join(parts)

    def check(self, state: dict) -> list:
        now = self.now_fn()
        if not (10 <= now.hour < 21):
            return []  # daytime judgment only
        slot = f"{now.strftime('%Y-%m-%d')}-{'AM' if now.hour < 14 else 'PM'}"
        if state.get("last_slot") == slot:
            return []  # at most one insight per half-day

        context = self._gather()
        if not context.strip():
            state["last_slot"] = slot
            return []

        recent = state.get("recent", [])
        from llm_router import get_llm
        prompt = self.PROMPT.format(
            recent="\n".join(f"- {r}" for r in recent) or "(nothing yet)",
            when=now.strftime('%A %-I%p'), context=context)
        try:
            resp = get_llm(temperature=0, tier="light").invoke(prompt)
        except Exception as e:
            print(f"[insight] llm failed: {e}")
            return []  # don't burn the slot on a transient failure
        content = resp.content
        if isinstance(content, list):
            content = next((b['text'] for b in content
                            if isinstance(b, dict) and b.get('type') == 'text'), str(content))
        try:
            verdict = _parse_llm_json(content)
        except Exception:
            print(f"[insight] unparseable verdict: {content[:160]}")
            return []

        state["last_slot"] = slot
        if not verdict.get("send") or not verdict.get("insight"):
            return []
        text = verdict["insight"].strip()
        state["recent"] = (recent + [text])[-8:]
        return [Notification("💡 " + text)]


class ReflectionMonitor(Monitor):
    """Learns from the day. Once each evening it reviews the day's working memory (what the
    user told Aria + actions taken) and PROPOSES durable standing rules worth adopting — a
    recurring preference, a correction, a pattern a sharp assistant would codify. Propose-
    only: the user replies yes/no and the agent adds it via add_standing_instruction, so
    nothing is auto-applied and behavior can't silently drift. (Facts are already consolidated
    into long-term memory by nightly compaction; this adds the behavioral-rule layer — the
    'learn from conversation' loop.)
    """

    PROMPT = """You are Aria reflecting on the day to get better at helping this user. Below is
today's working memory — things they told you and actions you took. Identify up to TWO DURABLE
behavioral rules worth adopting GOING FORWARD: a recurring preference, a correction they made,
or a pattern worth codifying (e.g. "always send the news as an email, never a chat", "default
reminders to 9am"). Default HARD to none — only a rule that is clearly useful, GENERAL (not a
one-off), and that the user would plausibly want standing. Do NOT duplicate an existing rule.

EXISTING STANDING RULES (don't duplicate):
{existing}

TODAY'S WORKING MEMORY:
{context}

Return ONLY JSON (no markdown):
{{"suggestions": ["<imperative rule sentence>"]}}  (use [] if nothing is worth proposing)"""

    def __init__(self, interval_seconds=3600, now_fn=None):
        super().__init__(name="reflection", interval_seconds=interval_seconds)
        self.now_fn = now_fn or datetime.now

    def _todays_memory(self) -> str:
        import memory
        try:
            with open(memory.SCRATCHPAD_PATH) as f:
                return "\n".join(line.strip() for line in f if line.strip())[-4000:]
        except Exception:
            return ""

    def check(self, state: dict) -> list:
        now = self.now_fn()
        if now.hour < 20:                       # reflect in the evening
            return []
        today = now.strftime('%Y-%m-%d')
        if state.get("last_date") == today:     # at most once per day
            return []
        context = self._todays_memory()
        if not context.strip():
            state["last_date"] = today
            return []
        try:
            from instructions import render_for_prompt
            existing = render_for_prompt() or "(none)"
        except Exception:
            existing = "(none)"

        from llm_router import get_llm
        prompt = self.PROMPT.format(existing=existing, context=context)
        try:
            resp = get_llm(temperature=0, tier="light").invoke(prompt)
        except Exception as e:
            print(f"[reflection] llm failed: {e}")
            return []   # don't burn the day on a transient failure
        content = resp.content
        if isinstance(content, list):
            content = next((b['text'] for b in content
                            if isinstance(b, dict) and b.get('type') == 'text'), str(content))
        try:
            verdict = _parse_llm_json(content)
        except Exception:
            print(f"[reflection] unparseable: {content[:160]}")
            return []

        state["last_date"] = today
        recent = state.get("recent", [])
        # Volume cap: at most ONE proposal per day and TWO per ISO week — "too many
        # learnings" erodes trust faster than a missed pattern (it resurfaces next week).
        week = self.now_fn().strftime('%G-W%V')
        if state.get("week") != week:
            state["week"], state["week_count"] = week, 0
        out = []
        for rule in (verdict.get("suggestions") or [])[:1]:
            rule = (rule or "").strip()
            if not rule or state.get("week_count", 0) >= 2:
                continue
            # Dedup against BOTH the adopted registry and prior proposals — by word overlap,
            # not exact match, so a paraphrase of last week's suggestion doesn't re-fire
            # (the same trip-prep rule was proposed 3x in different wording).
            if rule in recent or _rule_redundant(rule, existing) \
                    or _rule_redundant(rule, " ".join(recent)):
                continue
            recent.append(rule)
            state["week_count"] = state.get("week_count", 0) + 1
            out.append(Notification(
                "🧠 Learning from today — want this as a standing rule?\n"
                f"  “{rule}”\nReply yes to adopt it, or just ignore."))
        state["recent"] = recent[-12:]
        return out


class WeeklyReflectionMonitor(Monitor):
    """Longer-horizon reflection. Once a week (Sunday evening) it looks across the DURABLE
    signal — patterns in the commitment history + recent long-term memory — and surfaces ONE
    warm 'here's what I noticed this week' note, plus optionally one durable standing-rule
    proposal. Where the daily ReflectionMonitor sees only today, this catches patterns that
    only emerge over weeks. Propose-only; silent by default.
    """

    PROMPT = """You are Aria reflecting on the PAST WEEK to help this user better. Below is the
durable signal — patterns in their commitments and recent long-term memory. Optionally write
ONE short, warm observation worth sharing (a trend, a recurring slip, something shifting) —
the kind a thoughtful chief-of-staff notices over time. And optionally ONE durable standing
rule worth adopting. Default to little: skip the rule unless clearly useful and general; skip
the note unless something is genuinely worth saying. Never restate raw data.

EXISTING STANDING RULES (don't duplicate):
{existing}

THIS WEEK'S SIGNAL:
{context}

Return ONLY JSON (no markdown):
{{"note": "<one warm sentence, or empty>", "rule": "<imperative rule sentence, or empty>"}}"""

    def __init__(self, interval_seconds=3600, now_fn=None):
        super().__init__(name="weekly_reflection", interval_seconds=interval_seconds)
        self.now_fn = now_fn or datetime.now

    def _signal(self) -> str:
        parts = []
        try:
            from skills.commitment_manager import commitment_patterns
            findings = commitment_patterns().get("findings") or []
            if findings:
                parts.append("COMMITMENT PATTERNS:\n" + "\n".join(f"- {f}" for f in findings))
        except Exception as e:
            print(f"[weekly] commitments gather failed: {e}")
        try:
            import memory
            cs = os.path.join(os.path.dirname(memory.__file__), "cold_storage")
            files = sorted(f for f in os.listdir(cs)) if os.path.isdir(cs) else []
            if files:
                with open(os.path.join(cs, files[-1])) as fh:
                    parts.append("RECENT LONG-TERM MEMORY:\n" + fh.read()[-2000:])
        except Exception as e:
            print(f"[weekly] memory gather failed: {e}")
        return "\n\n".join(parts)

    def check(self, state: dict) -> list:
        now = self.now_fn()
        if now.weekday() != 6 or now.hour < 18:     # Sunday evening
            return []
        week = now.strftime('%Y-%U')
        if state.get("last_week") == week:
            return []
        context = self._signal()
        if not context.strip():
            state["last_week"] = week
            return []
        try:
            from instructions import render_for_prompt
            existing = render_for_prompt() or "(none)"
        except Exception:
            existing = "(none)"

        from llm_router import get_llm
        try:
            resp = get_llm(temperature=0, tier="light").invoke(
                self.PROMPT.format(existing=existing, context=context))
        except Exception as e:
            print(f"[weekly] llm failed: {e}")
            return []
        content = resp.content
        if isinstance(content, list):
            content = next((b['text'] for b in content
                            if isinstance(b, dict) and b.get('type') == 'text'), str(content))
        try:
            verdict = _parse_llm_json(content)
        except Exception:
            print(f"[weekly] unparseable: {content[:160]}")
            return []

        state["last_week"] = week
        out = []
        note = (verdict.get("note") or "").strip()
        if note:
            out.append(Notification("🗓️ Looking back on the week — " + note))
        rule = (verdict.get("rule") or "").strip()
        recent = state.get("recent", [])
        if rule and rule not in recent and not _rule_redundant(rule, existing) \
                and not _rule_redundant(rule, " ".join(recent)):
            recent.append(rule)
            out.append(Notification(
                "🧠 A pattern worth a standing rule?\n"
                f"  “{rule}”\nReply yes to adopt it, or just ignore."))
        state["recent"] = recent[-8:]
        return out


def _rule_redundant(rule: str, existing: str) -> bool:
    """True if a proposed standing rule substantially overlaps the EXISTING registry — so we
    never re-propose a rule the user already has (the 'asked for a rule already updated' bug).
    Deterministic content-word overlap; the LLM is also told the existing rules, but it can't
    be trusted to never restate one."""
    import re
    stop = {"the", "a", "an", "to", "of", "for", "and", "or", "is", "are", "be", "with", "on",
            "in", "when", "if", "his", "her", "my", "your", "always", "never", "by", "default",
            "from", "now", "should", "that", "this", "you", "him", "she"}
    def words(s):
        return {w for w in re.findall(r"[a-z]+", (s or "").lower())
                if w not in stop and len(w) > 2}
    rw = words(rule)
    if not rw:
        return False
    return len(rw & words(existing)) / len(rw) >= 0.6


class NetflixMonitor(Monitor):
    """Acts the moment a household-update email lands; the *report* can wait.

    This replaces the planned ngrok + Pub/Sub push pipeline (see ADR 0004): a few
    minutes of polling latency in exchange for zero public-facing infrastructure.
    """

    def __init__(self, interval_seconds=30):
        # 30s: someone is standing at the TV waiting. A messages.list every 30s is
        # ~5 Gmail quota units against a 15,000/min allowance — effectively free.
        super().__init__(name="netflix", interval_seconds=interval_seconds)

    def check(self, state: dict) -> list:
        from skills.netflix_manager import (get_netflix_gmail_service,
                                            update_netflix_household,
                                            HOUSEHOLD_EMAIL_QUERY)

        service = get_netflix_gmail_service()
        if not service:
            return []  # secondary token not set up — not an error worth nagging about

        resp = service.users().messages().list(
            userId='me', q=f"({HOUSEHOLD_EMAIL_QUERY}) newer_than:1d",
            maxResults=1).execute()
        messages = resp.get('messages', [])
        if not messages:
            return []

        msg_id = messages[0]['id']
        if msg_id == state.get("last_handled_id"):
            return []

        print(f"[engine] netflix: new household email {msg_id}, acting now")
        state["last_handled_id"] = msg_id
        result = update_netflix_household.invoke({})
        # The point of automating this is that the user is NOT bothered. Stay SILENT on a clean
        # success (Netflix sends these often — a ping each time is the bulk of the message flood);
        # only notify when it actually needs them (login wall, expired link, failure).
        if result.strip().startswith("✅"):
            print("[engine] netflix: handled cleanly — staying silent")
            return []
        return [Notification(
            f"📺 A Netflix household-update email arrived but I couldn't finish it:\n{result}")]


class ProactiveEngine:
    """Drives the monitors, enforces quiet hours, owns the notification queue."""

    def __init__(self, monitors, notify_fn=send_telegram, quiet_hours=(23, 8),
                 tick_seconds=30):
        self.monitors = monitors
        self.notify_fn = notify_fn
        self.quiet_start, self.quiet_end = quiet_hours
        self.tick_seconds = tick_seconds

    def in_quiet_hours(self, now: datetime = None) -> bool:
        hour = (now or datetime.now()).hour
        if self.quiet_start > self.quiet_end:  # window wraps midnight, e.g. 23 -> 8
            return hour >= self.quiet_start or hour < self.quiet_end
        return self.quiet_start <= hour < self.quiet_end

    def tick(self, now_monotonic: float = None, now: datetime = None):
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        state = load_state()
        new_notifications = []

        for monitor in self.monitors:
            if now_monotonic < monitor._next_due:
                continue
            monitor._next_due = now_monotonic + monitor.interval_seconds
            try:
                slice_ = state.setdefault(monitor.name, {})
                new_notifications.extend(monitor.check(slice_) or [])
            except Exception as e:
                # One broken monitor must never take down the engine or the bot.
                print(f"[engine] ⚠️ monitor '{monitor.name}' failed: {e}")

        queue = state.setdefault("queued_notifications", [])
        quiet = self.in_quiet_hours(now)

        for n in new_notifications:
            # Whatever happens to the *ping*, the action itself goes into Aria's
            # working memory so the chat agent can answer "what did you do?".
            _record_to_memory(n.text)
            if quiet and not n.urgent:
                print(f"[engine] quiet hours — queued: {n.text[:60]!r}")
                queue.append(n.text)
            else:
                self.notify_fn(n.text)

        if not quiet and queue:
            digest = "🌅 While you were away:\n\n" + "\n\n".join(f"• {t}" for t in queue)
            if self.notify_fn(digest):
                state["queued_notifications"] = []

        # Dedicated liveness heartbeat: proves the ENGINE itself ran this loop, independent
        # of whether any individual monitor succeeded (a revoked Gmail token must not make a
        # healthy engine look dead — that masks the real problem during triage).
        state["engine_tick_ts"] = time.time()

        save_state(state)

    def run_forever(self, stop_event: threading.Event = None):
        print(f"[engine] 🫀 Proactivity engine online — monitors: "
              f"{', '.join(m.name for m in self.monitors)} "
              f"(quiet hours {self.quiet_start:02d}:00–{self.quiet_end:02d}:00)")
        while not (stop_event and stop_event.is_set()):
            try:
                self.tick()
            except Exception as e:
                print(f"[engine] ⚠️ tick failed: {e}")
            time.sleep(self.tick_seconds)


def default_engine(notify_fn=send_telegram) -> ProactiveEngine:
    return ProactiveEngine(
        monitors=[CommitmentMonitor(), EmailDigestMonitor(), ChaseMonitor(),
                  InsightMonitor(), ReflectionMonitor(), WeeklyReflectionMonitor(),
                  ReplyResolveMonitor(), NetflixMonitor(), HealthMonitor(), HeartbeatMonitor()],
        notify_fn=notify_fn,
    )


def start_engine_thread(notify_fn=None) -> threading.Thread:
    """Start the engine as a daemon thread (used by telegram_bot.py).

    notify_fn lets the host process enrich delivery — the bot passes one that both
    telegrams the user AND appends the message to the conversation thread, so the
    agent's history matches what the user saw.
    """
    engine = default_engine(notify_fn=notify_fn or send_telegram)
    thread = threading.Thread(target=engine.run_forever, name="aria-engine", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    engine = default_engine()
    if "--once" in sys.argv:
        # Force every monitor due, run one tick, and exit — for manual testing.
        engine.tick()
        print("[engine] single tick complete")
    else:
        engine.run_forever()
