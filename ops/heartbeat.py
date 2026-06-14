"""External dead-man's-switch — proves the host is ALIVE to an outside monitor.

The internal health system (healthcheck.py + HealthMonitor) detects "something is
broken" but CANNOT detect "the whole host is dead" — a crashed process or powered-off
machine can't send a Telegram alert about itself. This closes that gap: the engine pings
an external monitor URL on a heartbeat. If the pings stop (process died, host off,
network down), the EXTERNAL service alerts you — the one failure mode Aria can't report
herself.

Setup (free): create a check at https://healthchecks.io, put its ping URL in .env as
HEARTBEAT_URL, and set its period to ~20m with ~10m grace (matches the 15m ping). No-op
when unset. Pinging URL/fail signals an unhealthy-but-alive state so the monitor sees
both liveness and health.
"""
import os

import requests

from dotenv import load_dotenv

load_dotenv()


def configured() -> bool:
    return bool(os.getenv("HEARTBEAT_URL"))


def send_heartbeat(healthy: bool = True, note: str = "") -> bool:
    """Ping the external monitor. healthy=False pings URL/fail (alive but degraded).
    Best-effort — never raises; a heartbeat that crashed the engine would be absurd."""
    url = os.getenv("HEARTBEAT_URL")
    if not url:
        return False
    target = url if healthy else url.rstrip('/') + '/fail'
    try:
        requests.post(target, data=note[:500], timeout=10)
        return True
    except Exception as e:
        print(f"[heartbeat] ping failed: {e}")
        return False
