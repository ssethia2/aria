"""Per-friend daily usage caps for the webvoice trial — so one guest can't run up the bill.

Counts + an estimated dollar spend live in a JSON file (webvoice/usage.json, gitignored),
bucketed by date → user → kind. Three knobs via env:
  ARIA_GUEST_DAILY_TOKENS  Live voice sessions/day per friend (the big cost lever)  [12]
  ARIA_GUEST_DAILY_AGENT   full-brain (/agent) calls/day per friend                 [60]
  ARIA_GUEST_DAILY_USD     overall $/day per friend — a hard ceiling across ALL     [5]
                           actions, whatever the mix

Two gates, either of which can refuse: a per-kind COUNT cap and an overall DOLLAR ceiling.
The dollar figures (COST below) are coarse ESTIMATES — a safety rail, not billing — leaning
slightly high so real spend stays under the ceiling. (Live audio is browser↔Google and isn't
metered here; sessions/day × per-session estimate bounds it.) Tune the per-action estimates
via env if your prices/usage differ.

Call pattern:
  - simple gate (mint a Live token): check_and_increment(uid, "tokens")  — atomic check+charge
  - two-phase (/agent, where the cheap fast-path vs the full brain isn't known up front):
      allow(uid, "quick"); … run fast path …; record(uid, "quick")   on a hit
      allow(uid, "agent"); … run full brain …; record(uid, "agent")  on escalation
"""
import os
import json
import threading
from datetime import date
from pathlib import Path

USAGE_PATH = Path(__file__).resolve().parent / "usage.json"
_lock = threading.Lock()


def _count_cap(kind: str):
    """Per-kind daily count cap, or None for kinds bounded only by the dollar ceiling."""
    if kind == "tokens":
        return int(os.getenv("ARIA_GUEST_DAILY_TOKENS", "12"))
    if kind == "agent":
        return int(os.getenv("ARIA_GUEST_DAILY_AGENT", "60"))
    return None


def _cost(kind: str) -> float:
    """Estimated $ for one action of `kind`. Coarse, deliberately a touch high."""
    return {
        "tokens": float(os.getenv("ARIA_COST_TOKENS", "0.15")),  # one Gemini Live session
        "agent":  float(os.getenv("ARIA_COST_AGENT", "0.06")),   # one full Opus brain call
        "quick":  float(os.getenv("ARIA_COST_QUICK", "0.002")),  # one light-tier fast reply
    }.get(kind, 0.0)


def _usd_cap() -> float:
    return float(os.getenv("ARIA_GUEST_DAILY_USD", "5"))


def _load() -> dict:
    try:
        return json.loads(USAGE_PATH.read_text())
    except Exception:
        return {}


def _slot(data: dict, user_id: str):
    """Prune to today's bucket (so the file can't grow unbounded) and return (bucket, user)."""
    today = date.today().isoformat()
    bucket = {today: data.get(today, {})}
    user = bucket[today].setdefault(user_id, {})
    return bucket, user


def _within(user: dict, kind: str) -> bool:
    """Would one more `kind` action stay within BOTH this friend's count cap and $ ceiling?"""
    cap = _count_cap(kind)
    if cap is not None and user.get(kind, 0) >= cap:
        return False
    return round(user.get("usd", 0.0) + _cost(kind), 6) <= _usd_cap()


def _charge(bucket: dict, user: dict, kind: str):
    user[kind] = user.get(kind, 0) + 1
    user["usd"] = round(user.get("usd", 0.0) + _cost(kind), 4)
    try:
        USAGE_PATH.write_text(json.dumps(bucket))
    except Exception:
        pass


def allow(user_id: str, kind: str) -> bool:
    """True if charging one `kind` action would stay within this friend's daily count cap
    and overall $ ceiling. Does NOT record — pair with record() after the action succeeds
    (lets the /agent path charge 'quick' vs 'agent' by what actually ran)."""
    with _lock:
        _, user = _slot(_load(), user_id)
        return _within(user, kind)


def record(user_id: str, kind: str):
    """Record one `kind` action: bump its count and add its estimated $ to today's spend."""
    with _lock:
        bucket, user = _slot(_load(), user_id)
        _charge(bucket, user, kind)


def check_and_increment(user_id: str, kind: str) -> bool:
    """Atomic gate for single-phase call sites: charge one `kind` if it stays within both
    caps and record it; return False (recording nothing) if either cap is already reached."""
    with _lock:
        bucket, user = _slot(_load(), user_id)
        if not _within(user, kind):
            return False
        _charge(bucket, user, kind)
        return True
