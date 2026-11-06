"""launchd entry point for the morning briefing (pure Python, no shell).

Exists because macOS TCC blocks /bin/bash from reading ~/Documents in launchd
context (run.sh died with "Operation not permitted", exit 126), while the Python
binary already holds that grant — the Telegram bot runs under it. Does what run.sh
does: nightly memory compaction (best-effort) and then the briefing (fail-loud).
run.sh remains for interactive use.

The schedule lives here, not in the plist. launchd wakes this every 15 minutes and it
runs once per day at BRIEFING_TIME in BRIEFING_TZ (default 08:00, host time). launchd
reads the clock in whatever timezone it booted with, so a plist hour drifts when the
laptop changes timezone: after a flight it kept firing on the old zone, and the next
reboot would have silently moved it.
"""
import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

STATE_PATH = os.path.join(os.path.dirname(__file__), "briefing_state.json")
STALE_AFTER = timedelta(hours=12)     # a briefing this late is skipped; the next one is near


def _zone():
    """BRIEFING_TZ as a zone, or None for the host's timezone."""
    name = os.getenv("BRIEFING_TZ", "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        print(f"BRIEFING_TZ={name!r} isn't a timezone name; using host time")
        return None


def scheduled_for(now=None) -> datetime:
    """The most recent briefing slot at or before `now`, as an aware datetime."""
    now = (now or datetime.now().astimezone()).astimezone(_zone())
    hour, minute = (int(x) for x in os.getenv("BRIEFING_TIME", "08:00").split(":"))
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at <= now else at - timedelta(days=1)


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def due(now=None) -> datetime | None:
    """The slot to run now, or None: already attempted, or too late to be worth it."""
    now = now or datetime.now().astimezone()
    at = scheduled_for(now)
    if load_state().get("slot") == at.date().isoformat():
        return None
    if now - at > STALE_AFTER:
        return None
    return at


def run():
    try:
        from nightly_compaction import compact_scratchpad
        compact_scratchpad()
    except Exception:
        # Compaction trouble shouldn't cost the user their briefing.
        print(f"Compaction failed (continuing to briefing):\n{traceback.format_exc()}")

    from main import run_with_alerts
    run_with_alerts()


def main(force=False):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    import redact
    redact.install()
    at = due()
    if at is None and not force:
        return
    at = at or scheduled_for()
    # One attempt per slot: a failure alerts once (run_with_alerts) rather than every
    # 15 minutes until morning.
    state = dict(load_state(), slot=at.date().isoformat())
    _save_state(state)
    print(f"\n=== Briefing for {at:%a %d %b %H:%M %Z} (started {datetime.now():%H:%M} host time) ===")
    run()
    _save_state(dict(state, last_ok_ts=datetime.now().timestamp()))


if __name__ == "__main__":
    main(force="--now" in sys.argv)
