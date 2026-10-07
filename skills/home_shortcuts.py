"""Home control through Apple Shortcuts — no Home Assistant required.

The bulbs/plugs already live in Apple Home. macOS ships a `shortcuts` CLI, so Aria can run
Shortcuts you've built in the Shortcuts app (using the "Control [Home]" action): "Aria
Lights Off", "Aria Movie Mode", ... This is the interim bridge until a real Home Assistant
box exists; it can run scenes but cannot read device state or host automations.

Safety: Aria may only run Shortcuts whose name starts with a prefix (default "Aria",
ARIA_SHORTCUT_PREFIX). Anything else on the Mac — "Open Note", personal Shortcuts — is
invisible to her. The CLI is invoked with an argument list (never a shell), so a shortcut
name from the model can't inject commands.

The first run of a Home-controlling shortcut may pop a one-time permission dialog on the
Mac ("Shortcuts wants to access your Home") — approve it once on the screen.
"""
import os
import subprocess

from langchain_core.tools import tool

RUN_TIMEOUT = 45   # seconds; a hung permission dialog shouldn't hang the agent


def _prefix() -> str:
    return (os.getenv("ARIA_SHORTCUT_PREFIX") or "Aria").strip()


def _exposed():
    """Shortcut names Aria may run (those starting with the prefix), sorted. None when the
    `shortcuts` CLI is unavailable (non-macOS) or errors."""
    try:
        r = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    p = _prefix().lower()
    return sorted(n.strip() for n in r.stdout.splitlines()
                  if n.strip().lower().startswith(p))


def _resolve(query: str, names: list):
    """(name, None) for a unique match, else (None, message). Exact (case-insensitive) wins;
    otherwise a unique substring match, so 'lights off' finds 'Aria Lights Off'."""
    q = (query or "").strip().lower()
    for n in names:
        if n.lower() == q:
            return n, None
    hits = [n for n in names if q and q in n.lower()]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, "Several home shortcuts match — which one? " + ", ".join(hits)
    return None, ("No home shortcut called '" + query + "'. Available: "
                  + (", ".join(names) if names else "(none yet)"))


@tool
def list_home_shortcuts() -> str:
    """List the home-control Shortcuts Aria can run (lights, scenes, etc. built in the Mac's
    Shortcuts app and named with the 'Aria' prefix). Use this when asked what she can do
    around the house, or before run_home_shortcut if unsure of the exact name."""
    names = _exposed()
    if names is None:
        return ("I can't reach Apple Shortcuts here (needs macOS with the `shortcuts` "
                "command).")
    if not names:
        return (f"No home shortcuts set up yet. Create Shortcuts in the Shortcuts app whose "
                f"names start with '{_prefix()}' (e.g. '{_prefix()} Lights Off') using the "
                f"'Control Home' action, and I can run them.")
    return "Home shortcuts I can run:\n" + "\n".join(f"- {n}" for n in names)


@tool
def run_home_shortcut(name: str) -> str:
    """Run a home-control Shortcut by name — turn lights on/off, activate a scene, etc.
    Use this for smart-home requests when Home Assistant isn't connected (control_light
    says so). Accepts a partial name ("lights off" finds "Aria Lights Off"). If you're not
    sure what exists, call list_home_shortcuts first.

    Args:
        name: The shortcut's name, or a unique part of it.
    """
    names = _exposed()
    if names is None:
        return ("I can't reach Apple Shortcuts here (needs macOS with the `shortcuts` "
                "command).")
    target, problem = _resolve(name, names)
    if not target:
        return problem
    try:
        r = subprocess.run(["shortcuts", "run", target], capture_output=True, text=True,
                           timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return (f"'{target}' didn't finish in {RUN_TIMEOUT}s — there may be a permission "
                f"prompt waiting on the Mac's screen (Shortcuts asking for Home access).")
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:200]
        return f"'{target}' failed: {err or 'unknown error'}"
    out = (r.stdout or "").strip()
    return f"Ran '{target}'." + (f" It returned: {out[:300]}" if out else "")
