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


class BrainUnavailable(Exception):
    """The subscription turn produced no usable reply (rate limit, CLI/auth failure) —
    the caller should serve this turn from the API fallback agent instead of dying."""


CONTEXT_TURNS = 10   # mirror turns bridged into a fallback turn's prompt


def _recent_context(thread_id: str, limit: int = CONTEXT_TURNS) -> str:
    """The last few conversation turns from the unified mirror (both engines write it) —
    the bridge that lets a fallback turn pick up mid-conversation."""
    with _lock:
        mirror = _load_state().get("threads", {}).get(thread_id, {}).get("messages", [])
    return "\n".join(
        f"{'User' if m.get('type') == 'human' else 'Aria'}: {(m.get('content') or '')[:400]}"
        for m in mirror[-limit:])


def _queue_unsynced(thread_id: str, user_text: str, reply: str):
    """Record a fallback-served exchange so the NEXT subscription turn can absorb it
    (the SDK session never saw it)."""
    with _lock:
        state = _load_state()
        th = state.setdefault("threads", {}).setdefault(thread_id, {})
        th.setdefault("unsynced", []).append(
            {"you": user_text[:400], "aria": (reply or "")[:400]})
        th["unsynced"] = th["unsynced"][-8:]
        _save_state(state)


_fallback = {"agent": None, "alerted": None}


def _api_fallback_agent():
    """Lazily built LangGraph/API agent used when the subscription can't serve a turn
    (force_api so this can never recurse back into the SDK brain)."""
    if _fallback["agent"] is None:
        from agent_core import build_agent, open_checkpointer
        _fallback["agent"] = build_agent(checkpointer=open_checkpointer(),
                                         guest=False, force_api=True)
    return _fallback["agent"]


def classify_failure(reason: str) -> str:
    """'auth' | 'limit' | 'other' from the CLI's failure text — so the owner alert can say
    what to actually DO (re-login vs wait) instead of a generic shrug."""
    r = (reason or "").lower()
    if any(s in r for s in ("usage limit", "rate limit", "limit reached", "limit will reset",
                            "out of extra usage", "resets at")):
        return "limit"
    if any(s in r for s in ("login", "log in", "logged out", "authentication", "auth",
                            "credential", "api key", "unauthorized", "oauth", "expired",
                            "invalid_grant", "revoked", "please run /login")):
        return "auth"
    return "other"


def _fallback_alert(reason: str):
    """Tell the owner (once/day) that turns are being served on API billing — hitting the
    subscription limit should never silently turn back into a surprise API bill. The
    message names the likely cause and the fix (re-login vs wait it out)."""
    print(f"[brain] ⚠️ subscription turn failed ({reason}) — serving from the API fallback")
    from datetime import date
    today = date.today().isoformat()
    if _fallback["alerted"] == today:
        return
    _fallback["alerted"] = today
    kind = classify_failure(reason)
    if kind == "auth":
        advice = ("Looks like the Claude Code login expired — run /login in Claude Code "
                  "on the Mac and I'll switch back on your next message.")
    elif kind == "limit":
        advice = ("Looks like the subscription's rolling usage limit — nothing to do; "
                  "I'll switch back automatically once it resets.")
    else:
        advice = "I'll retry the subscription on each new message."
    try:
        from notify import send_telegram
        send_telegram("⚠️ Heads up: I couldn't use the subscription just now, so I'm "
                      f"answering on API billing for the moment. {advice}\n"
                      f"(Reason: {reason[:150]})")
    except Exception:
        pass


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
            th = state.get("threads", {}).get(thread_id, {})
            session = th.get("session_id")
            unsynced = list(th.get("unsynced", []))

        # Bridge: exchanges served by the API fallback while the subscription was limited
        # are absorbed into this turn's prompt, then cleared on success — so the SDK
        # session regains continuity instead of a hole.
        prompt = text
        if unsynced:
            gap = "\n".join(f"User: {u['you']}\nAria: {u['aria']}" for u in unsynced)
            prompt = (f"<missed_context>While you were unavailable, these exchanges were "
                      f"handled for you (already answered — context only, do not re-answer):"
                      f"\n{gap}</missed_context>\n\n{text}")

        result = {"reply": None, "session_id": session}

        async def go():
            opts = self._options(resume=session)
            async for msg in query(prompt=prompt, options=opts):
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
                    result["is_error"] = getattr(msg, "is_error", False)
                    result["subtype"] = getattr(msg, "subtype", "") or ""
                    # The CLI's actual words ("Claude AI usage limit reached…", "Invalid
                    # API key", …). The SDK's structured error can be junk (empty errors +
                    # subtype "success" on error results), so THIS is the diagnostic.
                    result["result_text"] = str(getattr(msg, "result", "") or "")[:300]
                    self._record_usage(msg)

        # The SDK MERGES its env option over the parent environment (verified live:
        # apiKeySource=ANTHROPIC_API_KEY leaked through _clean_env alone), so the key must
        # be absent from the PROCESS env while the CLI spawns. Pop + restore. The narrow
        # window can make a concurrent engine LLM init skip its Anthropic link for one
        # call (falls through to its next provider) — non-fatal by design.
        key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            anyio.run(go)
        except Exception as e:
            # Prefer the CLI's own result text (captured before it exited) — the SDK's
            # replacement exception can be uninformative ("error result: success").
            raise BrainUnavailable(result.get("result_text") or str(e)[:200])
        finally:
            if key is not None:
                os.environ["ANTHROPIC_API_KEY"] = key

        # No usable reply (rate limit, auth, CLI failure) → let the caller serve this
        # turn from the API fallback instead of answering "(no response)".
        if not result["reply"]:
            raise BrainUnavailable(result.get("result_text") or result.get("subtype")
                                   or "no reply from subscription")

        reply = result["reply"]
        with _lock:
            state = _load_state()
            th = state.setdefault("threads", {}).setdefault(thread_id, {})
            th["session_id"] = result["session_id"]
            th["unsynced"] = []          # the gap is absorbed — clear only on success
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

    @staticmethod
    def _bridged_payload(text: str, ctx: str):
        """The fallback turn's payload, carrying recent cross-engine context so the API
        agent picks up mid-conversation instead of amnesiac."""
        if not ctx:
            return {"messages": [HumanMessage(content=text)]}
        return {"messages": [HumanMessage(content=(
            f"<recent_conversation>For continuity — the most recent exchanges (from a "
            f"session this thread can't see):\n{ctx}</recent_conversation>\n\n{text}"))]}

    def invoke(self, payload, config=None):
        text = self._text_of(payload)
        thread = self._thread_of(config)
        try:
            reply = self._run_turn(thread, text)
            return {"messages": [AIMessage(content=reply)]}
        except BrainUnavailable as e:
            _fallback_alert(str(e))
            ctx = _recent_context(thread)          # read BEFORE this turn hits the mirror
            out = _api_fallback_agent().invoke(self._bridged_payload(text, ctx), config)
            try:
                from agent_core import extract_text
                reply_text = extract_text(out["messages"][-1].content)
                _queue_unsynced(thread, text, reply_text)   # next sub turn absorbs the gap
                self.update_state(config, {"messages": [
                    HumanMessage(content=text), AIMessage(content=reply_text)]})
            except Exception:
                pass
            return out

    def stream(self, payload, config=None, stream_mode="updates"):
        """Generator matching LangGraph's updates stream closely enough for the bots'
        progress notes: tool events yield messages carrying .tool_calls, then the final
        text yields as a plain AIMessage. On subscription failure the whole turn streams
        from the API fallback agent (its chunks ARE native LangGraph updates)."""
        events = []
        text = self._text_of(payload)
        thread = self._thread_of(config)
        try:
            reply = self._run_turn(thread, text,
                                   on_tool=lambda name: events.append(name))
        except BrainUnavailable as e:
            _fallback_alert(str(e))
            from agent_core import extract_text
            ctx = _recent_context(thread)
            final = None
            for chunk in _api_fallback_agent().stream(self._bridged_payload(text, ctx),
                                                      config=config, stream_mode=stream_mode):
                for _node, update in (chunk or {}).items():
                    if isinstance(update, dict):
                        for m in update.get("messages", []) or []:
                            if isinstance(m, AIMessage) and not (getattr(m, "tool_calls", None) or []):
                                t = extract_text(m.content)
                                if t and t.strip():
                                    final = t
                yield chunk
            if final:
                _queue_unsynced(thread, text, final)
                self.update_state(config, {"messages": [
                    HumanMessage(content=text), AIMessage(content=final)]})
            return
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
