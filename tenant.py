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
_current_name: contextvars.ContextVar = contextvars.ContextVar("current_name", default=None)


def set_current_user(user_id, name=None):
    """Set the current guest (+ optional display name) for this context. Returns a token to
    pass to reset_current_user()."""
    return (_current_user.set(user_id), _current_name.set(name))


def reset_current_user(token):
    user_tok, name_tok = token
    _current_user.reset(user_tok)
    _current_name.reset(name_tok)


def get_current_user():
    return _current_user.get()


def get_current_name():
    """The current guest's display name, if set (so Aria can greet them by name)."""
    return _current_name.get()


def is_guest() -> bool:
    """True when a guest user is set (i.e. NOT the owner / default context)."""
    return _current_user.get() is not None


def safe_id(user_id: str) -> str:
    """A ChromaDB-safe collection suffix for a user id (alphanumerics + underscore)."""
    return re.sub(r"[^a-zA-Z0-9]", "_", str(user_id))[:48] or "anon"
