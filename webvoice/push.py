"""Web Push for guest reminders — so a reminder a guest sets actually pings their phone.

VAPID keys come from env (VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY / VAPID_CLAIM_EMAIL).
Per-user push subscriptions live in webvoice/push_subs.json (gitignored). A background
scheduler reuses commitment_manager.get_pingable_now() under each subscribed user's tenant
and sends a push when something's due, deduping per day like the owner engine does.

Note: on iOS, web push only works for a PWA that's been Added to Home Screen (iOS 16.4+).
Android/desktop Chrome work in a normal tab.
"""
import os
import json
import time
import threading
from pathlib import Path
from datetime import datetime

from pywebpush import webpush, WebPushException

HERE = Path(__file__).resolve().parent
SUBS_PATH = HERE / "push_subs.json"      # { "<user_id>": [ <subscription>, ... ] }
STATE_PATH = HERE / "push_state.json"    # { "<YYYY-MM-DD>": { "<user_id>": [pinged ids] } }
_lock = threading.Lock()
_started = False


def public_key() -> str:
    return os.getenv("VAPID_PUBLIC_KEY", "")


def _vapid_private() -> str:
    return os.getenv("VAPID_PRIVATE_KEY", "")


def _claims() -> dict:
    return {"sub": os.getenv("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")}


def _read(path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write(path, data):
    try:
        path.write_text(json.dumps(data))
    except Exception:
        pass


def add_subscription(user_id: str, sub: dict):
    """Store a push subscription for a user (replacing any with the same endpoint)."""
    ep = sub.get("endpoint")
    with _lock:
        data = _read(SUBS_PATH)
        kept = [s for s in data.get(user_id, []) if s.get("endpoint") != ep]
        kept.append(sub)
        data[user_id] = kept
        _write(SUBS_PATH, data)


def user_ids() -> list:
    return list(_read(SUBS_PATH).keys())


def send(user_id: str, title: str, body: str, url: str = "/") -> int:
    """Push to all of a user's devices; prune subscriptions the browser has expired."""
    priv = _vapid_private()
    if not priv:
        return 0
    subs = _read(SUBS_PATH).get(user_id, [])
    if not subs:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url})
    alive, sent = [], 0
    for s in subs:
        try:
            webpush(subscription_info=s, data=payload,
                    vapid_private_key=priv, vapid_claims=_claims())
            alive.append(s)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):        # subscription gone — drop it
                continue
            alive.append(s)               # transient failure — keep for next time
        except Exception:
            alive.append(s)
    with _lock:
        data = _read(SUBS_PATH)
        data[user_id] = alive
        _write(SUBS_PATH, data)
    return sent


def _tick(now=None):
    """One scheduler pass: for each subscribed user, push anything due now (deduped per day,
    recurring reminders advanced) — the push twin of engine.CommitmentMonitor, per tenant."""
    import tenant
    from skills.commitment_manager import get_pingable_now, advance_recurring

    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    state = _read(STATE_PATH)
    day = state.get(today, {})

    for uid in user_ids():
        notified = set(day.get(uid, []))
        tok = tenant.set_current_user(uid)
        try:
            due = get_pingable_now(now)
        finally:
            tenant.reset_current_user(tok)
        for c in due:
            recurring = c.get("is_recurring")
            if c["id"] in notified and not recurring:
                continue
            when = f" — {c['due_time']}" if c.get("due_time") else ""
            send(uid, "⏰ Reminder", f"{c['description']}{when}")
            if recurring:
                tok = tenant.set_current_user(uid)
                try:
                    advance_recurring(c["id"], today)
                finally:
                    tenant.reset_current_user(tok)
            else:
                notified.add(c["id"])
        day[uid] = sorted(notified)

    _write(STATE_PATH, {today: day})      # keep only today's bucket


def _loop(interval: int):
    while True:
        try:
            _tick()
        except Exception as e:
            print(f"[push] scheduler error: {e}")
        time.sleep(interval)


def start_scheduler(interval: int = 60):
    """Start the background reminder-push loop once (no-op if VAPID isn't configured)."""
    global _started
    if _started or not (public_key() and _vapid_private()):
        return
    _started = True
    threading.Thread(target=_loop, args=(interval,), daemon=True).start()
    print("[push] reminder scheduler started")
