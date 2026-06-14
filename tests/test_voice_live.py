"""Tests for the Gemini Live conversational mode's non-audio plumbing.

The live session itself needs a mic + network, so we only cover the parts we can:
the escalation tool declaration and the brain bridge. Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock

import voice_live


class TestVoiceLive(unittest.TestCase):
    def test_escalate_declaration(self):
        d = voice_live.ESCALATE
        self.assertEqual(d.name, "escalate_to_aria")
        self.assertIn("request", d.parameters.properties)
        self.assertIn("request", d.parameters.required)

    def test_run_brain_invokes_agent_and_extracts_text(self):
        agent = MagicMock()
        agent.invoke.return_value = {"messages": [MagicMock(content="On it — done.")]}
        out = voice_live.run_brain(agent, "remind me to call mom")
        agent.invoke.assert_called_once()
        # the user's request is forwarded into the agent
        sent = agent.invoke.call_args.args[0]["messages"][0].content
        self.assertEqual(sent, "remind me to call mom")
        self.assertEqual(out, "On it — done.")


if __name__ == "__main__":
    unittest.main()
