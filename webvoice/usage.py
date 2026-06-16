"""Per-friend daily usage caps for the webvoice trial — so one guest can't run up the bill.

Counts live in a JSON file (webvoice/usage.json, gitignored), bucketed by date → user →
kind. Two knobs via env:
  ARIA_GUEST_DAILY_TOKENS  Live sessions/day per friend (the big cost lever)  [default 12]
  ARIA_GUEST_DAILY_AGENT   brain (/agent) calls/day per friend                [default 60]

Coarse on purpose — a safety rail, not billing. (Live *minutes* aren't metered here since
the audio is browser↔Google; sessions/day × Gemini's per-session cap bounds the spend.)
"""
import os
import json
import threading
from datetime import date
from pathlib import Path

USAGE_PATH = Path(__file__).resolve().parent / "usage.json"
_lock = threading.Lock()


def _cap(kind: str) -> int:
    return int(os.getenv(f"ARIA_GUEST_DAILY_{kind.upper()}",
                         "12" if kind == "tokens" else "60"))


def _load() -> dict:
    try:
        return json.loads(USAGE_PATH.read_text())
    except Exception:
        return {}


def check_and_increment(user_id: str, kind: str) -> bool:
    """Count one use of `kind` ('tokens' or 'agent') for this friend today. Returns True if
    still within the daily cap (and records it), False if the cap is already reached."""
    today = date.today().isoformat()
    with _lock:
        data = _load()
        # keep only today's bucket so the file can't grow unbounded
        data = {today: data.get(today, {})}
        user = data[today].setdefault(user_id, {})
        if user.get(kind, 0) >= _cap(kind):
            return False
        user[kind] = user.get(kind, 0) + 1
        try:
            USAGE_PATH.write_text(json.dumps(data))
        except Exception:
            pass
        return True
