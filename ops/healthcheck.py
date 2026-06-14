"""Self-diagnosis — turn silent failures loud.

The #1 reliability gap (the briefing failed silently for days). This runs a fast
battery of checks over config, credentials, state freshness, and disk, returning
OK / WARN / FAIL per check. Used three ways:
  1. get_system_status tool   — the user can ask "are you healthy?"
  2. HealthMonitor (engine.py) — alerts proactively when something turns FAIL
  3. CLI (`python3 healthcheck.py`) — the "doctor" a self-hoster runs to debug
     their install; exits non-zero on FAIL (scriptable in a systemd/launchd timer)

Honest limit: this runs *inside* Aria, so it can't detect "the whole host is
dead" — that needs an external dead-man's-switch (see README → Reliability).
"""
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_EMOJI = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
BASE = os.path.dirname(os.path.dirname(__file__))


def _p(*parts):
    return os.path.join(BASE, *parts)


def _check(name, fn):
    """Run one check, never let it raise — a broken check is itself a FAIL."""
    try:
        status, detail = fn()
        return (name, status, detail)
    except Exception as e:
        return (name, FAIL, f"check errored: {e}")


def check_secrets():
    missing = [k for k in ('TELEGRAM_BOT_TOKEN', 'ANTHROPIC_API_KEY') if not os.getenv(k)]
    if missing:
        return FAIL, f"missing: {', '.join(missing)}"
    if not os.getenv('GEMINI_API_KEY'):
        return WARN, "GEMINI_API_KEY unset — fallback chain has no final tier"
    if not os.getenv('TELEGRAM_ALLOWED_CHAT_ID'):
        return WARN, "no allowlist — bot is in setup mode, won't answer"
    return OK, "core secrets present"


def check_email():
    from integrations import email_backend
    if email_backend.using_app_password():
        ok, detail = email_backend.check_login()
        return (OK if ok else FAIL), detail
    if not os.path.exists(_p('token.json')):
        return WARN, "no email configured (set EMAIL_APP_PASSWORD, or token.json via auth_google.py)"
    from skills.email_manager import get_gmail_service
    service = get_gmail_service()
    if not service:
        return FAIL, "Gmail service wouldn't build (token expired? re-auth)"
    service.users().getProfile(userId='me').execute()  # cheap live call
    return OK, "Gmail reachable (API)"


def check_memory():
    from core import memory
    if memory.collection is None:
        return WARN, "ChromaDB uninitialized — semantic memory disabled"
    return OK, f"semantic memory OK ({memory.collection.count()} vectors)"


def check_databases():
    cal = _p('aria_calendar.db')
    if os.path.exists(cal):
        sqlite3.connect(cal).execute("SELECT 1 FROM commitments LIMIT 1").fetchone()
    ckpt = "checkpoints present" if os.path.exists(_p('aria_checkpoints.db')) else "no checkpoints yet"
    return OK, f"databases readable ({ckpt})"


def check_engine_freshness():
    """engine_state.json's email-digest timestamp proves the engine is ticking."""
    import json
    path = _p('engine_state.json')
    if not os.path.exists(path):
        return WARN, "engine hasn't run yet (no state file)"
    with open(path) as f:
        state = json.load(f)
    ts = state.get('email-digest', {}).get('last_check_ts')
    if not ts:
        return WARN, "engine state has no recent tick"
    age_min = (time.time() - ts) / 60
    if age_min > 60:
        return FAIL, f"engine last ticked {age_min:.0f} min ago — is the bot running?"
    return OK, f"engine ticking ({age_min:.0f} min ago)"


def check_briefing_today():
    """After 9am, today's briefing report should exist — catches the silent-fail bug."""
    now = datetime.now()
    if now.hour < 9:
        return OK, "before 9am — briefing not due yet"
    report = _p('reports', f"daily_summary_{now.strftime('%Y-%m-%d')}.md")
    if os.path.exists(report):
        return OK, "today's briefing ran"
    return WARN, "no briefing report for today — did the 08:00 job fire?"


def check_heartbeat():
    from ops import heartbeat
    if heartbeat.configured():
        return OK, "external dead-man's-switch active"
    return WARN, "no HEARTBEAT_URL — a host crash won't alert you (see README → Reliability)"


def check_disk():
    free_mb = shutil.disk_usage(BASE).free / 1e6
    if free_mb < 200:
        return FAIL, f"only {free_mb:.0f}MB free"
    if free_mb < 1000:
        return WARN, f"{free_mb:.0f}MB free — getting low"
    return OK, f"{free_mb/1000:.1f}GB free"


_CHECKS = [
    ("secrets", check_secrets),
    ("email", check_email),
    ("memory", check_memory),
    ("databases", check_databases),
    ("engine", check_engine_freshness),
    ("briefing", check_briefing_today),
    ("heartbeat", check_heartbeat),
    ("disk", check_disk),
]


def run_all():
    return [_check(name, fn) for name, fn in _CHECKS]


def worst(results) -> str:
    statuses = {r[1] for r in results}
    return FAIL if FAIL in statuses else WARN if WARN in statuses else OK


def summary(results) -> str:
    head = {OK: "All systems healthy.", WARN: "Healthy, with warnings.",
            FAIL: "Something needs attention."}[worst(results)]
    lines = [f"{_EMOJI[s]} {name}: {detail}" for name, s, detail in results]
    return head + "\n" + "\n".join(lines)


if __name__ == '__main__':
    results = run_all()
    print(summary(results))
    sys.exit(1 if worst(results) == FAIL else 0)
