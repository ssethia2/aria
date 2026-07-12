"""Subscription brain (Claude Agent SDK facade): LangGraph-shaped surface, tool bridging,
guest isolation, env hygiene, and API fallback. Offline — the SDK's query() is mocked.

Run: python3 -m unittest discover tests
"""
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        # os.environ itself during the call — and back afterwards.
        seen = {}

        def fake_anyio_run(fn):
            seen["key_during_call"] = os.environ.get("ANTHROPIC_API_KEY", "ABSENT")

        b = SubscriptionBrain(tools=[fake_weather])
        with tempfile.TemporaryDirectory() as d, \
             patch.object(brain_sdk, "STATE_PATH", Path(d) / "s.json"), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret"}), \
             patch.object(brain_sdk.anyio, "run", fake_anyio_run):
            b._run_turn("t", "hi")
            self.assertEqual(seen["key_during_call"], "ABSENT")     # gone during the call
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-secret")  # restored


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
