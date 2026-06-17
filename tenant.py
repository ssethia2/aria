"""Principals — the uniform identity model that lets one Aria serve many users.

EVERY execution context runs as a `Principal` (a `user_id` + a `role`). There is no
"absence of a tenant means full power" special case: the owner is simply the `OWNER`
principal (`user_id="owner"`, role `owner`), and a friend trying Aria out is a `guest`
principal carrying their own id (+ optional display name). Capability differences are
driven by the principal's ROLE, never by whether an id happens to be set — so a new code
path can't accidentally inherit owner access just by forgetting to check.

Storage is keyed by `user_id` uniformly: the owner's commitments live under tenant
"owner", each guest's under their own id; a guest's memory routes to their OWN ChromaDB
collection (`mem_<id>`). Friends can never see the owner's data or each other's.

Two carriers:
  - the ContextVar principal (`current_principal()`) drives role-shaped behaviour the
    model prompt needs (guest banner, name) — things the run config can't reach.
  - the run config (`scope_from_config`) is the RELIABLE cross-thread carrier the data
    tools read, and it FAILS CLOSED: a guest call we can't attribute refuses rather than
    falling back to owner data.

Usage for a guest request (caller owns set/reset, always in try/finally):
    token = set_current_user("alice", name="Alice")
    try:
        agent.invoke(...)
    finally:
        reset_current_user(token)
"""
import re
import contextvars
from dataclasses import dataclass

ROLE_OWNER = "owner"
ROLE_GUEST = "guest"
OWNER_ID = "owner"


@dataclass(frozen=True)
class Principal:
    """Who a request runs as. `user_id` keys their data; `role` gates their capabilities."""
    user_id: str
    role: str
    name: str = None


# The owner's own interfaces (telegram/voice/cron/engine) run as this principal by default;
# they're single-tenant processes. The guest web backend sets a guest principal per request.
OWNER = Principal(OWNER_ID, ROLE_OWNER)

_principal: contextvars.ContextVar = contextvars.ContextVar("principal", default=OWNER)


def set_current_user(user_id, name=None):
    """Enter a GUEST context as `user_id` (+ optional display name) for this execution
    context. Returns a token to pass to reset_current_user()."""
    return _principal.set(Principal(user_id, ROLE_GUEST, name))


def reset_current_user(token):
    """Restore the principal that was current before the matching set_current_user()."""
    _principal.reset(token)


def current_principal() -> Principal:
    return _principal.get()


def get_current_user():
    """The current principal's user_id ("owner" for the owner, the guest id for a guest)."""
    return _principal.get().user_id


def get_current_name():
    """The current guest's display name, if set (so Aria can greet them by name)."""
    return _principal.get().name


def is_guest() -> bool:
    """True when the current principal is a guest (role-driven, NOT 'is an id set')."""
    return _principal.get().role == ROLE_GUEST


def is_owner() -> bool:
    """True when the current principal is the owner."""
    return _principal.get().role == ROLE_OWNER


def tenant_from_config(config):
    """(user_id, is_guest) from a LangChain run config's `configurable`. Config is the
    RELIABLE carrier (LangChain propagates it across threads), so tenant-aware tools read
    it from here and fail closed — never falling back to owner data — when a guest call
    arrives without a tenant."""
    cfg = (config or {}).get("configurable", {}) or {}
    return cfg.get("tenant"), bool(cfg.get("guest"))


def scope_from_config(config):
    """Establish the principal for a tool call from the run config (the reliable carrier),
    fail-closed. Returns (token_or_None, refusal_or_None): on a refusal the caller MUST stop
    (a guest call we can't attribute — never serve owner/other data); otherwise reset the
    token (if any) in a finally."""
    uid, guest = tenant_from_config(config)
    if guest and not uid:
        return None, "I couldn't identify your session, so I won't read or write your data."
    return (set_current_user(uid) if uid else None), None


def safe_id(user_id: str) -> str:
    """A ChromaDB-safe collection suffix for a user id (alphanumerics + underscore)."""
    return re.sub(r"[^a-zA-Z0-9]", "_", str(user_id))[:48] or "anon"
