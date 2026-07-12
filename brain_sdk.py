"""Subscription brain — Aria's interactive agent on the Claude Agent SDK.

Runs the OWNER's chat turns through Claude Code's login (Max subscription) instead of
API-billed LangChain calls: the same ~$0.06/cold-turn Sonnet work now draws from the
subscription's quota. Enabled with ARIA_BRAIN=subscription in .env; anything that fails
here falls back to the LangGraph/API brain in agent_core.build_agent().

Scope (the hybrid split, deliberate):
  - OWNER interactive brain only. The engine + quick fast-path stay on cheap API models,
    and GUESTS always get the API LangGraph agent — strangers must never draw from the
    owner's personal subscription (and a 24/7 autonomous engine on a consumer sub is
    ToS-gray; pennies on Haiku anyway).
  - The spawned CLI runs WITHOUT ANTHROPIC_API_KEY in its env: with a key present it
    silently bills the API instead of the subscription (probe: apiKeySource must be none).

Shape: a LangGraph-compatible facade — invoke / stream / get_state / update_state — so
telegram_bot, imessage_bot, interact, voice_live and webvoice owner-mode work unchanged.
Aria's LangChain tools are auto-bridged into an in-process SDK MCP server (no per-tool
rewrites); Claude Code's own built-in tools are disabled so her toolset stays exactly hers.
Conversation continuity uses SDK session resume (per thread), plus a small local history
mirror to serve get_state/update_state (the fast-path's context + persistence).
"""
import os
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
from langchain_core.messages import AIMessage, HumanMessage

STATE_PATH = Path(os.path.join(os.path.dirname(__file__), "brain_sdk_state.json"))
_lock = threading.Lock()
MIRROR_MAX = 40          # per-thread history mirror cap (fast-path context only)
MODEL = os.getenv("ARIA_SDK_MODEL", "sonnet")
MAX_TURNS = 30


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict):
    try:
        import token_store
        token_store.atomic_write_text(STATE_PATH, json.dumps(state))
    except Exception as e:
        print(f"[brain] state save failed: {e}")


def _clean_env() -> dict:
    """Subprocess env WITHOUT the API key — its presence flips billing to the API."""
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


_alerted = {"date": None}


def _billing_alert(source: str):
    """A subscription-brain turn billed the API — the exact silent regression this whole
    feature exists to prevent. Log every time; Telegram the owner once per day."""
    print(f"[brain] ⚠️ apiKeySource={source} — this turn billed the API, not the subscription!")
    from datetime import date
    today = date.today().isoformat()
    if _alerted["date"] == today:
        return
    _alerted["date"] = today
    try:
        from notify import send_telegram
        send_telegram("⚠️ Heads up: my subscription brain is billing the API "
                      f"(apiKeySource={source}). Check Claude Code login / ARIA_BRAIN setup.")
    except Exception:
        pass


def _bridge_tool(lc_tool):
    """Wrap one LangChain tool as an SDK MCP tool (schema + name straight off the tool)."""
    from claude_agent_sdk import tool as sdk_tool

    schema = {"type": "object", "properties": dict(lc_tool.args or {})}

    async def handler(args, _lc=lc_tool):
        try:
            result = await anyio.to_thread.run_sync(lambda: _lc.invoke(dict(args or {})))
        except Exception as e:
            result = f"Tool error: {e}"
        return {"content": [{"type": "text", "text": str(result)}]}

    return sdk_tool(lc_tool.name, (lc_tool.description or lc_tool.name), schema)(handler)


class SubscriptionBrain:
    """LangGraph-shaped facade over the Claude Agent SDK. Owner-only."""

    def __init__(self, tools=None):
        from claude_agent_sdk import create_sdk_mcp_server  # raises if SDK missing
        if tools is None:
            from agent_core import build_tools
            tools = build_tools(guest=False)
        self._tool_names = [t.name for t in tools]
        self._server = create_sdk_mcp_server(
            name="aria", version="1.0.0", tools=[_bridge_tool(t) for t in tools])

    # ---- options / prompt -------------------------------------------------------

    def _system_prompt(self) -> str:
        from agent_core import build_system_message
        content = build_system_message().content
        if isinstance(content, list):
            return "\n\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        return str(content)

    def _options(self, resume=None):
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeAgentOptions(
            system_prompt=self._system_prompt(),
            mcp_servers={"aria": self._server},
            allowed_tools=[f"mcp__aria__{n}" for n in self._tool_names],
            # Claude Code's own tools OFF — Aria's toolset stays exactly Aria's.
            disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                              "WebFetch", "WebSearch", "NotebookEdit", "Task",
                              "TodoWrite", "KillShell", "BashOutput"],
            permission_mode="bypassPermissions",   # unattended tool runs (same as LangGraph)
            model=MODEL,
            max_turns=MAX_TURNS,
            env=_clean_env(),
            cwd=os.path.dirname(__file__),
            resume=resume,
        )

    # ---- core turn --------------------------------------------------------------

    def _run_turn(self, thread_id: str, text: str, on_tool=None):
        """One agent turn via the SDK. Returns final reply text. Emits tool names to
        on_tool as they happen (for progress notes)."""
        from claude_agent_sdk import query

        with _lock:
            state = _load_state()
            session = state.get("threads", {}).get(thread_id, {}).get("session_id")

        result = {"reply": None, "session_id": session}

        async def go():
            opts = self._options(resume=session)
            async for msg in query(prompt=text, options=opts):
                t = type(msg).__name__
                if t == "SystemMessage" and getattr(msg, "subtype", "") == "init":
                    sid = msg.data.get("session_id")
                    if sid:
                        result["session_id"] = sid
                    src = msg.data.get("apiKeySource")
                    if src and src != "none":
                        _billing_alert(src)   # NOT on the subscription — never silent
                elif t == "AssistantMessage":
                    for b in msg.content:
                        if hasattr(b, "text") and getattr(b, "text", "").strip():
                            result["reply"] = b.text
                        name = getattr(b, "name", None)
                        if name and on_tool:
                            on_tool(name.replace("mcp__aria__", ""))
                elif t == "ResultMessage":
                    sid = getattr(msg, "session_id", None)
                    if sid:
                        result["session_id"] = sid
                    self._record_usage(msg)

        # The SDK MERGES its env option over the parent environment (verified live:
        # apiKeySource=ANTHROPIC_API_KEY leaked through _clean_env alone), so the key must
        # be absent from the PROCESS env while the CLI spawns. Pop + restore. The narrow
        # window can make a concurrent engine LLM init skip its Anthropic link for one
        # call (falls through to its next provider) — non-fatal by design.
        key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            anyio.run(go)
        finally:
            if key is not None:
                os.environ["ANTHROPIC_API_KEY"] = key

        reply = result["reply"] or "(no response)"
        with _lock:
            state = _load_state()
            th = state.setdefault("threads", {}).setdefault(thread_id, {})
            th["session_id"] = result["session_id"]
            mirror = th.setdefault("messages", [])
            mirror.extend([{"type": "human", "content": text},
                           {"type": "ai", "content": reply}])
            th["messages"] = mirror[-MIRROR_MAX:]
            _save_state(state)
        return reply

    @staticmethod
    def _record_usage(result_msg):
        """Log the turn to cost_tracker under a subscription label (prices as $0 — the
        point — but calls/tokens stay visible in get_costs)."""
        try:
            import cost_tracker
            u = getattr(result_msg, "usage", None) or {}
            cost_tracker.record(f"subscription/{MODEL}",
                                input_tokens=u.get("input_tokens", 0),
                                output_tokens=u.get("output_tokens", 0),
                                cache_read=u.get("cache_read_input_tokens", 0),
                                cache_creation=u.get("cache_creation_input_tokens", 0))
        except Exception:
            pass

    # ---- LangGraph-compatible surface -------------------------------------------

    @staticmethod
    def _thread_of(config) -> str:
        return ((config or {}).get("configurable", {}) or {}).get("thread_id", "default")

    @staticmethod
    def _text_of(payload) -> str:
        msgs = (payload or {}).get("messages", [])
        return getattr(msgs[-1], "content", "") if msgs else ""

    def invoke(self, payload, config=None):
        reply = self._run_turn(self._thread_of(config), self._text_of(payload))
        return {"messages": [AIMessage(content=reply)]}

    def stream(self, payload, config=None, stream_mode="updates"):
        """Generator matching LangGraph's updates stream closely enough for the bots'
        progress notes: tool events yield messages carrying .tool_calls, then the final
        text yields as a plain AIMessage."""
        events = []
        reply = self._run_turn(self._thread_of(config), self._text_of(payload),
                               on_tool=lambda name: events.append(name))
        for name in events:
            yield {"agent": {"messages": [
                AIMessage(content="", tool_calls=[{"name": name, "args": {},
                                                   "id": f"note-{name}"}])]}}
        yield {"agent": {"messages": [AIMessage(content=reply)]}}

    def get_state(self, config):
        with _lock:
            mirror = _load_state().get("threads", {}) \
                .get(self._thread_of(config), {}).get("messages", [])
        msgs = [HumanMessage(content=m["content"]) if m.get("type") == "human"
                else AIMessage(content=m["content"]) for m in mirror]
        return SimpleNamespace(values={"messages": msgs})

    def update_state(self, config, update):
        entries = []
        for m in (update or {}).get("messages", []):
            kind = "human" if isinstance(m, HumanMessage) else "ai"
            entries.append({"type": kind, "content": getattr(m, "content", "") or ""})
        if not entries:
            return
        with _lock:
            state = _load_state()
            th = state.setdefault("threads", {}).setdefault(self._thread_of(config), {})
            th["messages"] = (th.get("messages", []) + entries)[-MIRROR_MAX:]
            _save_state(state)
