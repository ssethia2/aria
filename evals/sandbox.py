"""Eval sandbox — run the REAL agent against throwaway state.

The agent under test is the production one (same system prompt, same tool names,
descriptions and schemas, same brain — subscription or API). What changes is what the
tools are wired to:

  - Every tool is a proxy carrying the real tool's exact name/description/schema, so the
    model's view is identical to production. Every call is recorded (name + args + turn)
    for assertions.
  - A small allowlist of tools whose state is purely LOCAL (commitments, people, standing
    rules, groceries) run for REAL against temp copies of their stores — so scenarios can
    assert on what actually landed in the database, not just on what was called.
  - EVERYTHING ELSE — email, calendar, web, weather, Spotify, lights, the browser, memory,
    news, Netflix, system status — is FAKED with a canned result and never reaches its
    real implementation. New tools are faked by default (allowlist, not denylist), so a
    tool added later can never touch a real account from an eval run.

Process-level side channels are sealed too: the subscription brain's state file and the
LangGraph checkpointer point at throwaway stores, and Telegram notifications (billing /
fallback alerts) are swallowed instead of pinging your phone.
"""
import functools
import json
import os
import shutil
import tempfile
import threading
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")

# Tools that run for real — each one's only state is a local file/DB the sandbox redirects.
REAL_TOOLS = frozenset({
    "add_commitment", "list_commitments", "complete_commitment", "drop_commitment",
    "reschedule_commitment", "snooze_commitment", "reopen_commitment", "analyze_commitments",
    "remember_person", "get_person", "list_people",
    "add_standing_instruction", "update_standing_instruction", "remove_standing_instruction",
    "add_to_grocery_list", "view_grocery_list", "remove_from_grocery_list",
    "clear_grocery_list",
})

_HA_NOT_CONNECTED = ("Smart home isn't connected yet — set HA_URL and HA_TOKEN in .env "
                     "(Home Assistant).")

# Canned results for faked tools. {arg} placeholders are filled from the call's args
# (missing ones render empty). Scenarios override any of these per-tool.
DEFAULT_FAKES = {
    "add_memory": "Saved to memory.",
    "search_memory": "No relevant memories found.",
    "read_cold_storage": "No such file in cold storage.",
    "update_memory": "Profile updated: {key} = {value}",
    "read_and_summarize_emails": "Processed 3 emails. Summary generated.",
    "read_email_thread": "No matching email thread found for '{search_query}'.",
    "draft_email_reply": "Draft created in Gmail (not sent) — it's waiting for your review.",
    "generate_morning_news": "News briefing generated successfully.",
    "update_netflix_household": "No pending Netflix household-update emails.",
    "create_calendar_event": "Created '{title}' on {date_iso} {start_time} ({calendars}).",
    "get_calendar_events": "Nothing on the calendar in that window.",
    "update_calendar_event": "Updated the event matching '{query}'.",
    "delete_calendar_event": "Deleted the event matching '{query}'.",
    "list_my_calendars": ("Calendars:\n- primary (personal)\n"
                          "- Satvik & Ashley (shared) — configured as the shared calendar"),
    "configure_shared_calendar": "Shared calendar saved.",
    "get_weather": "Today: sunny, high 68°F / low 52°F, light wind.",
    "web_search": "No results found for '{query}'.",
    "fetch_webpage": "Couldn't fetch {url}.",
    "create_note": "Created note '{title}'.",
    "append_to_note": "Appended to note '{title_query}'.",
    "search_notes": "No notes matched '{query}'.",
    "read_note": "No note found matching that title.",
    "check_packages": "No shipping or delivery emails found.",
    "browse_and_report": ("Explored the page and stopped before any payment or login. "
                          "Link: https://example.com"),
    "list_lights": _HA_NOT_CONNECTED,
    "control_light": _HA_NOT_CONNECTED,
    "list_home_shortcuts": ("Home shortcuts I can run:\n- Aria Lights Off\n"
                            "- Aria Lights On\n- Aria Movie Mode"),
    "run_home_shortcut": "Ran '{name}'.",
    "play_music": "Playing the best match for '{query}' on Spotify.",
    "playback_control": "Done.",
    "now_playing": "Nothing is playing right now.",
    "create_playlist": "Created playlist '{name}'.",
    "get_system_status": "Healthy — all checks passed.",
    "get_costs": "Estimated LLM spend — last 7 day(s): $1.20",
}
GENERIC_FAKE = "Done."


class _Blank(dict):
    def __missing__(self, key):
        return ""


def _render(template: str, args: dict) -> str:
    try:
        return " ".join(template.format_map(_Blank(args)).split()) if "{" in template \
            else template
    except (ValueError, IndexError):
        return template


class Sandbox:
    """Context manager: throwaway stores + proxied tools + sealed side channels.

    One Sandbox per scenario trial — state never leaks between scenarios.
        with Sandbox(overrides={...}) as sb:
            agent = sb.build_agent(brain="subscription")
            ...
            sb.calls   # [{"tool", "args", "turn"}]
    """

    def __init__(self, overrides=None, live_context=False):
        self.overrides = dict(overrides or {})
        self.live_context = live_context
        self.calls = []
        self.fallbacks = []          # subscription->API fallback reasons recorded this run
        self.turn = 0
        self._counts = {}
        self._lock = threading.Lock()
        self._patches = []
        self.dir = None
        self.tools = None

    # ---- lifecycle --------------------------------------------------------------

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="aria-eval-")
        src = ROOT if self.live_context else FIXTURES
        for name in ("profile.json", "instructions.json", "people.json"):
            p = os.path.join(src, name)
            if os.path.exists(p):
                shutil.copy(p, os.path.join(self.dir, name))
            else:
                with open(os.path.join(self.dir, name), "w") as f:
                    json.dump({} if name == "profile.json" else [], f)

        import agent_core
        import brain_sdk
        import cost_tracker  # noqa: F401  (import before patching so paths resolve)
        import instructions
        import memory
        import notify
        import people
        from langgraph.checkpoint.memory import InMemorySaver
        from skills import commitment_manager, grocery_manager, google_calendar

        d = self.dir
        self._start(commitment_manager, "DB_PATH", os.path.join(d, "commitments.db"))
        self._start(people, "PEOPLE_PATH", os.path.join(d, "people.json"))
        self._start(instructions, "INSTRUCTIONS_PATH", os.path.join(d, "instructions.json"))
        self._start(grocery_manager, "LIST_PATH", os.path.join(d, "grocery_list.json"))
        self._start(memory, "PROFILE_PATH", os.path.join(d, "profile.json"))
        self._start(google_calendar, "CONFIG_PATH", os.path.join(d, "calendar_config.json"))

        # Subscription brain: throwaway session state, no Telegram alerts, fresh fallback.
        from pathlib import Path
        self._start(brain_sdk, "STATE_PATH", Path(os.path.join(d, "brain_state.json")))
        self._start(brain_sdk, "_fallback", {"agent": None, "alerted": None})
        self._start(brain_sdk, "_fallback_alert", self._record_fallback)
        self._start(brain_sdk, "_billing_alert",
                    lambda src: self.fallbacks.append(f"billed the API (apiKeySource={src})"))
        self._start(notify, "send_telegram", lambda *a, **k: True)

        # The agent's tools and conversation store.
        self.tools = [self._proxy(t) for t in agent_core.build_tools(guest=False)]
        self._start(agent_core, "build_tools", self._build_tools)
        self._start(agent_core, "open_checkpointer", lambda: InMemorySaver())
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._patches.clear()
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def _start(self, target, attr, value):
        p = patch.object(target, attr, value)
        p.start()
        self._patches.append(p)

    def _build_tools(self, guest=False):
        if guest:
            raise RuntimeError("evals exercise the owner agent only")
        return list(self.tools)

    def _record_fallback(self, reason):
        self.fallbacks.append(str(reason))

    # ---- agent ------------------------------------------------------------------

    def build_agent(self, brain="env"):
        """The production agent wired to this sandbox. brain: 'subscription' | 'api' |
        'env' (whatever ARIA_BRAIN in .env says — what Telegram actually runs)."""
        import agent_core
        from langgraph.checkpoint.memory import InMemorySaver
        if brain == "subscription":
            # patch.dict edits the REAL os.environ in place (restored on exit). Never swap
            # os.environ for a plain dict: brain_sdk pops ANTHROPIC_API_KEY from it so the
            # CLI can't bill the API, and only the real mapping unsets it process-wide.
            p = patch.dict(os.environ, {"ARIA_BRAIN": "subscription"})
            p.start()
            self._patches.append(p)
        return agent_core.build_agent(checkpointer=InMemorySaver(),
                                      force_api=(brain == "api"))

    # ---- tool proxying ----------------------------------------------------------

    def _proxy(self, t):
        name = t.name
        orig = t.func
        sandbox = self

        @functools.wraps(orig)
        def wrapper(*args, **kwargs):
            call_args = {k: v for k, v in kwargs.items() if k != "config"}
            with sandbox._lock:
                sandbox.calls.append({"tool": name, "args": call_args, "turn": sandbox.turn})
                n = sandbox._counts.get(name, 0)
                sandbox._counts[name] = n + 1
            if name in sandbox.overrides:
                out = sandbox.overrides[name]
                if isinstance(out, list):
                    out = out[min(n, len(out) - 1)] if out else GENERIC_FAKE
                return _render(str(out), call_args)
            if name in REAL_TOOLS:
                return orig(*args, **kwargs)
            return _render(DEFAULT_FAKES.get(name, GENERIC_FAKE), call_args)

        return t.model_copy(update={"func": wrapper})

    # ---- state inspection -------------------------------------------------------

    def commitments(self):
        """Every commitment row in the sandbox store (any status), as dicts."""
        import sqlite3
        from skills import commitment_manager as cm
        if not os.path.exists(cm.DB_PATH):
            return []
        conn = sqlite3.connect(cm.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT id, description, kind, who, due_date, due_time, status, "
                "snoozed_until FROM commitments ORDER BY id").fetchall()
        finally:
            conn.close()
        keys = ["id", "description", "kind", "who", "due_date", "due_time", "status",
                "snoozed_until"]
        return [dict(zip(keys, r)) for r in rows]

    def standing_rules(self):
        import instructions
        return [i["instruction"] for i in instructions._load()]
