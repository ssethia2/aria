# ADR 0009 — Proactivity as an engine: moments, legwork first, one gate

**Status:** Accepted (2026-11). Builds on [ADR 0004](0004-polling-engine-over-webhooks.md); the polling engine stands.

## Context
Aria's proactivity was a set of independent monitors, each reacting to something that had
already happened (new mail, a commitment coming due) and each with its own ad-hoc limits. Three
gaps showed up in use:

1. **Nothing looked ahead.** A rescheduled interview sat in the commitments list as "reply to
   the invite" with no date. Nothing would have spoken before the meeting, and the invite was
   set in another timezone from where he was traveling.
2. **The judges didn't share context.** The chase and insight prompts never saw his standing
   rules (a rule like "never mention the surprise party" bound the chat agent only), and no
   monitor knew what another had just said.
3. **Offers couldn't be accepted.** Engine messages were mirrored into the conversation
   history, but a same-day SDK session is resumed rather than rebuilt from that history. When he
   answered a nudge, the session had never seen it.

We looked at how Muse (Meta) and Instinct handle proactivity. Little of their mechanics is
public, but the patterns are consistent:
- run on schedules and events, and come back when something changes or needs approval;
- do the work before speaking;
- suggest one concrete next step and act once approved;
- decide whether a result is worth a notification at all.

The best-documented failure is the opposite of silence: early users turned Muse's proactive
mode off because it pinged about email too often. Instinct's documented failure is acting
without approval.

## Decision
1. **Moments** (`moments.py`): each timed event on his personal calendar becomes a moment
   with touchpoints:
   - a meeting: an hour before, and the morning after;
   - a flight: the day before;
   - a reservation: three hours before.

   A touchpoint that lands in quiet hours moves to the next morning if that still leaves enough
   lead time, otherwise to the evening before.

   New kinds of moments are a classifier branch plus a `TOUCHPOINTS` entry.
2. **Legwork first.** At a touchpoint, `gather()` collects:
   - the email thread with the people involved, excluding calendar-system mail (`.ics`);
   - open commitments that name them;
   - what Aria knows about them.

   A moment with nothing to add skips the model call.
3. **One judge per touchpoint** (`MomentMonitor`, light tier). It states *why* before deciding.
   On a small model that turned silence on clear cases into correct heads-ups. Its rules:
   - Google's own reminder already exists, so speak only to add something.
   - The calendar event is the source of truth for time and join link.
   - Give at most one offer.
4. **A hard check on join links.** A Zoom or Meet link in a message must appear on the event or
   in the mail that was read, or the message is dropped. During development the judge once gave
   a cancelled meeting's link as the current one. A wrong link an hour before an interview is
   worse than no message.
5. **One gate.** `Notification.kind` separates nudges (Aria speaking on her own judgment:
   chase, insight, moments) from reports (reminders he set, digests, results, alerts a rule
   asked for).
   - Nudges share a daily budget (`NUDGE_BUDGET`).
   - Every delivered message goes into a 48-hour "told" log.
   - `judgment_context()` gives every judge the standing rules and that log.
6. **Offers reach the conversation.** Engine messages are queued as notes on the thread and
   prepended to the next turn of a resumed SDK session (`<sent_since_your_last_turn>`), so
   "yes" lands on a session that knows what it offered.

## Consequences
- Adding a proactive behavior means producing moments or notifications. Budget, dedup, rules,
  quiet-hours timing and conversation continuity come with it.
- Light-tier calls: about two per meeting with someone, one per flight or reservation. A
  calendar list call every five minutes is free.
- The budget is a backstop, first come first served. On a day that exhausts it, a later
  heads-up can be held. Judges are told to default to silence, so this should be rare.
- The source of truth is his personal calendar. Events only on the shared calendar are someone
  else's, per standing rule, and are ignored.
- Times are in the host's timezone, which follows him when he travels with the laptop. A host
  that stays home while he travels would get times wrong.
- The rule against asking "how did it go" is prompt-level; repeat offenders would need a
  code guard like the join-link check.
