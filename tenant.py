"""Per-request tenant context — the seam that lets one Aria serve isolated guest users.

The OWNER (you) runs with NO tenant set: everything behaves exactly as before — your
profile, your memory collection, your scratchpad, your full toolset.

A GUEST (a friend trying it out) is set as the current user for the duration of a request.
While set, memory routes to that guest's OWN ChromaDB collection (`mem_<user>`), the static
profile is empty, and the agent is built with a restricted, account-free toolset. Friends
can never see your data or each other's.

Usage (the caller owns set/reset, always in a try/finally so context can't leak):
    token = set_current_user("alice")
    try:
        agent.invoke(...)
    finally:
        reset_current_user(token)
"""
import re
import contextvars

_current_user: contextvars.ContextVar = contextvars.ContextVar("current_user", default=None)


def set_current_user(user_id):
    """Set the current guest for this context. Returns a token for reset_current_user()."""
    return _current_user.set(user_id)


def reset_current_user(token):
    _current_user.reset(token)


def get_current_user():
    return _current_user.get()


def is_guest() -> bool:
    """True when a guest user is set (i.e. NOT the owner / default context)."""
    return _current_user.get() is not None


def safe_id(user_id: str) -> str:
    """A ChromaDB-safe collection suffix for a user id (alphanumerics + underscore)."""
    return re.sub(r"[^a-zA-Z0-9]", "_", str(user_id))[:48] or "anon"
