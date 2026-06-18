"""Tests for the quick-answer fast path.

Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

import telegram_bot as tb


def _agent_with_history(messages=None):
    agent = MagicMock()
    state = MagicMock()
    state.values = {"messages": messages or []}
    agent.get_state.return_value = state
    return agent


class TestQuickAnswer(unittest.TestCase):
    def _llm(self, text):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=text)
        return llm

    def test_general_question_answered_directly(self):
        agent = _agent_with_history()
        with patch('agent_core.get_llm', return_value=self._llm("Lisbon.")):
            ans = tb.quick_answer(agent, "c1", "what's the capital of Portugal?")
        self.assertEqual(ans, "Lisbon.")
        agent.update_state.assert_called_once()      # turn persisted for context

    def test_escalates_for_tool_or_personal(self):
        agent = _agent_with_history()
        with patch('agent_core.get_llm', return_value=self._llm("ESCALATE")):
            ans = tb.quick_answer(agent, "c1", "what's on my calendar tomorrow?")
        self.assertIsNone(ans)                        # → full agent
        agent.update_state.assert_not_called()

    def test_escalates_on_llm_error(self):
        agent = _agent_with_history()
        llm = MagicMock(); llm.invoke.side_effect = Exception("down")
        with patch('agent_core.get_llm', return_value=llm):
            self.assertIsNone(tb.quick_answer(agent, "c1", "anything"))

    def test_empty_response_escalates(self):
        agent = _agent_with_history()
        with patch('agent_core.get_llm', return_value=self._llm("   ")):
            self.assertIsNone(tb.quick_answer(agent, "c1", "x"))

    def test_current_date_injected_into_prompt(self):
        # The fast path must be date-anchored, or it confuses today/tomorrow/day-of-week.
        from datetime import datetime
        agent = _agent_with_history()
        llm = self._llm("ESCALATE")
        with patch('agent_core.get_llm', return_value=llm):
            tb.quick_answer(agent, "c1", "what day is it?")
        sent_prompt = llm.invoke.call_args[0][0]
        self.assertIn(datetime.now().strftime('%Y-%m-%d'), sent_prompt)


if __name__ == '__main__':
    unittest.main()
