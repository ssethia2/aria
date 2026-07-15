"""Subscription brain (Claude Agent SDK facade): LangGraph-shaped surface, tool bridging,
guest isolation, env hygiene, and API fallback. Offline — the SDK's query() is mocked.

Run: python3 -m unittest discover tests
"""
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool as lc_tool

import brain_sdk
from brain_sdk import SubscriptionBrain


@lc_tool
def fake_weather(city: str) -> str:
    """Get weather for a city."""
    return f"Sunny in {city}"


def _brain():
    return SubscriptionBrain(tools=[fake_weather])


class TempState(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self._p = patch.object(brain_sdk, "STATE_PATH", Path(self.d.name) / "state.json")
        self._p.start()

    def tearDown(self):
        self._p.stop(); self.d.cleanup()


class TestFacade(TempState):
    def test_invoke_returns_langgraph_shape_and_mirrors_history(self):
        b = _brain()
        with patch.object(SubscriptionBrain, "_run_turn", return_value="It's sunny.") as rt:
            out = b.invoke({"messages": [HumanMessage(content="weather?")]},
                           config={"configurable": {"thread_id": "t1"}})
        self.assertEqual(out["messages"][-1].content, "It's sunny.")
        rt.assert_called_once_with("t1", "weather?")

    def test_get_update_state_roundtrip(self):
        b = _brain()
        cfg = {"configurable": {"thread_id": "t2"}}
        b.update_state(cfg, {"messages": [HumanMessage(content="hi"),
                                          AIMessage(content="hello")]})
        msgs = b.get_state(cfg).values["messages"]
        self.assertEqual([m.content for m in msgs], ["hi", "hello"])
        self.assertIsInstance(msgs[0], HumanMessage)
        self.assertIsInstance(msgs[1], AIMessage)

    def test_stream_yields_tool_notes_then_reply(self):
        b = _brain()

        def fake_run(thread, text, on_tool=None):
            on_tool("get_weather")
            return "Sunny."

        with patch.object(SubscriptionBrain, "_run_turn", side_effect=fake_run):
            chunks = list(b.stream({"messages": [HumanMessage(content="w?")]},
                                   config={"configurable": {"thread_id": "t3"}}))
        # first chunk: a message carrying .tool_calls (progress note)
        first = chunks[0]["agent"]["messages"][0]
        self.assertEqual(first.tool_calls[0]["name"], "get_weather")
        # last chunk: plain AI text = the reply
        last = chunks[-1]["agent"]["messages"][0]
        self.assertEqual(last.content, "Sunny.")
        self.assertFalse(getattr(last, "tool_calls", []))

    def test_mirror_capped(self):
        b = _brain()
        cfg = {"configurable": {"thread_id": "t4"}}
        for i in range(60):
            b.update_state(cfg, {"messages": [AIMessage(content=str(i))]})
        self.assertEqual(len(b.get_state(cfg).values["messages"]), brain_sdk.MIRROR_MAX)


class TestBridging(unittest.TestCase):
    def test_lc_tool_bridged_with_name_and_schema(self):
        captured = {}

        def fake_sdk_tool(name, desc, schema):
            captured.update(name=name, desc=desc, schema=schema)
            return lambda f: ("bridged", f)

        with patch("claude_agent_sdk.tool", fake_sdk_tool):
            brain_sdk._bridge_tool(fake_weather)
        self.assertEqual(captured["name"], "fake_weather")
        self.assertIn("city", captured["schema"]["properties"])

    def test_env_strips_api_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret"}):
            env = brain_sdk._clean_env()
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_run_turn_pops_key_from_process_env_and_restores(self):
        # The SDK MERGES env over the parent process env, so the key must be gone from
        # os.environ itself during the call — and back afterwards (even when the turn
        # produces no reply and raises BrainUnavailable).
        seen = {}

        def fake_anyio_run(fn):
            seen["key_during_call"] = os.environ.get("ANTHROPIC_API_KEY", "ABSENT")

        b = SubscriptionBrain(tools=[fake_weather])
        with tempfile.TemporaryDirectory() as d, \
             patch.object(brain_sdk, "STATE_PATH", Path(d) / "s.json"), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret"}), \
             patch.object(brain_sdk.anyio, "run", fake_anyio_run):
            with self.assertRaises(brain_sdk.BrainUnavailable):
                b._run_turn("t", "hi")           # no reply collected -> unavailable
            self.assertEqual(seen["key_during_call"], "ABSENT")     # gone during the call
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-secret")  # restored


class TestRuntimeFallback(TempState):
    """Subscription rate limit / failure mid-flight -> the turn serves from the API agent."""

    def _api(self, reply="api says hi"):
        api = MagicMock()
        api.invoke.return_value = {"messages": [AIMessage(content=reply)]}
        api.stream.return_value = iter([{"agent": {"messages": [AIMessage(content=reply)]}}])
        return api

    def test_invoke_falls_back_to_api_and_alerts(self):
        b = _brain()
        api = self._api()
        cfg = {"configurable": {"thread_id": "fb1"}}
        with patch.object(SubscriptionBrain, "_run_turn",
                          side_effect=brain_sdk.BrainUnavailable("rate_limited")), \
             patch.object(brain_sdk, "_api_fallback_agent", return_value=api), \
             patch.object(brain_sdk, "_fallback_alert") as alert:
            out = b.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
        self.assertEqual(out["messages"][-1].content, "api says hi")
        api.invoke.assert_called_once()               # served by the API agent
        alert.assert_called_once()                    # and the owner is told (never silent)
        # fast-path context mirror stays fresh even on fallback turns
        mirrored = [m.content for m in b.get_state(cfg).values["messages"]]
        self.assertEqual(mirrored, ["hello", "api says hi"])

    def test_stream_falls_back_to_api_chunks(self):
        b = _brain()
        api = self._api("streamed reply")
        with patch.object(SubscriptionBrain, "_run_turn",
                          side_effect=brain_sdk.BrainUnavailable("limit")), \
             patch.object(brain_sdk, "_api_fallback_agent", return_value=api), \
             patch.object(brain_sdk, "_fallback_alert"):
            chunks = list(b.stream({"messages": [HumanMessage(content="x")]},
                                   config={"configurable": {"thread_id": "fb2"}}))
        self.assertEqual(chunks[-1]["agent"]["messages"][0].content, "streamed reply")

    def test_error_result_raises_unavailable(self):
        # A ResultMessage with no assistant reply (e.g. usage-limit error) must raise,
        # not answer "(no response)".
        def fake_run(fn):
            pass    # go() never collects a reply
        b = _brain()
        with patch.object(brain_sdk.anyio, "run", fake_run):
            with self.assertRaises(brain_sdk.BrainUnavailable):
                b._run_turn("t", "hi")

    def test_force_api_bypasses_subscription_brain(self):
        import agent_core
        with patch.dict(os.environ, {"ARIA_BRAIN": "subscription"}):
            agent = agent_core.build_agent(force_api=True)
        self.assertNotIsInstance(agent, SubscriptionBrain)


class TestHistoryBridge(TempState):
    """The two one-way bridges: fallback turns see recent subscription context; the next
    subscription turn absorbs the fallback exchanges."""

    def test_fallback_prompt_carries_recent_conversation(self):
        b = _brain()
        cfg = {"configurable": {"thread_id": "hb1"}}
        # seed the mirror with subscription-era turns
        b.update_state(cfg, {"messages": [HumanMessage(content="remind me about the dentist"),
                                          AIMessage(content="Tracked for Friday 3pm.")]})
        api = MagicMock()
        api.invoke.return_value = {"messages": [AIMessage(content="fallback reply")]}
        with patch.object(SubscriptionBrain, "_run_turn",
                          side_effect=brain_sdk.BrainUnavailable("limit")), \
             patch.object(brain_sdk, "_api_fallback_agent", return_value=api), \
             patch.object(brain_sdk, "_fallback_alert"):
            b.invoke({"messages": [HumanMessage(content="when was that again?")]}, config=cfg)
        sent = api.invoke.call_args[0][0]["messages"][0].content
        self.assertIn("<recent_conversation>", sent)
        self.assertIn("Tracked for Friday 3pm.", sent)     # sub-era context bridged in
        self.assertIn("when was that again?", sent)

    def test_next_subscription_turn_absorbs_fallback_gap_then_clears(self):
        b = _brain()
        cfg = {"configurable": {"thread_id": "hb2"}}
        api = MagicMock()
        api.invoke.return_value = {"messages": [AIMessage(content="It's Friday at 3pm.")]}
        with patch.object(SubscriptionBrain, "_run_turn",
                          side_effect=brain_sdk.BrainUnavailable("limit")), \
             patch.object(brain_sdk, "_api_fallback_agent", return_value=api), \
             patch.object(brain_sdk, "_fallback_alert"):
            b.invoke({"messages": [HumanMessage(content="when is the dentist?")]}, config=cfg)

        # subscription comes back: the SDK prompt must carry the missed exchange
        prompts = []

        def fake_query(prompt=None, options=None):
            prompts.append(prompt)
            class _Gen:
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    raise StopAsyncIteration
            return _Gen()

        def fake_anyio_run(fn):
            import asyncio
            asyncio.run(fn())

        with patch("claude_agent_sdk.query", fake_query), \
             patch.object(brain_sdk.anyio, "run", fake_anyio_run):
            with self.assertRaises(brain_sdk.BrainUnavailable):
                b._run_turn("hb2", "thanks!")     # no reply from fake query — but prompt sent
        self.assertIn("<missed_context>", prompts[0])
        self.assertIn("It's Friday at 3pm.", prompts[0])   # the fallback exchange
        self.assertIn("thanks!", prompts[0])

        # unsynced survives a FAILED sub turn (cleared only on success)
        with patch.object(brain_sdk, "STATE_PATH", brain_sdk.STATE_PATH):
            import json
            st = json.loads(brain_sdk.STATE_PATH.read_text())
        self.assertTrue(st["threads"]["hb2"]["unsynced"])


class TestRouting(unittest.TestCase):
    def test_guest_never_gets_subscription_brain(self):
        import agent_core
        with patch.dict(os.environ, {"ARIA_BRAIN": "subscription"}):
            agent = agent_core.build_agent(guest=True)
        self.assertNotIsInstance(agent, SubscriptionBrain)

    def test_flag_off_uses_langgraph(self):
        import agent_core
        with patch.dict(os.environ, {"ARIA_BRAIN": ""}):
            agent = agent_core.build_agent()
        self.assertNotIsInstance(agent, SubscriptionBrain)

    def test_sdk_failure_falls_back_to_api_brain(self):
        import agent_core
        with patch.dict(os.environ, {"ARIA_BRAIN": "subscription"}), \
             patch.object(SubscriptionBrain, "__init__", side_effect=RuntimeError("no CLI")):
            agent = agent_core.build_agent()   # must not raise
        self.assertNotIsInstance(agent, SubscriptionBrain)


if __name__ == "__main__":
    unittest.main()
