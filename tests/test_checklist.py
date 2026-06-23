"""Telegram interactive commitment checklist: keyboard build + tap-to-complete callback.
Offline (commitment_manager + Telegram HTTP mocked). Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import patch, MagicMock

import telegram_bot as tb


class TestChecklistMarkup(unittest.TestCase):
    def test_one_button_per_open_commitment(self):
        items = [{"id": 3, "description": "Reply to Rohan", "due_date": "2026-07-01", "due_time": None},
                 {"id": 5, "description": "Renew passport", "due_date": None, "due_time": None}]
        with patch("skills.commitment_manager.get_open_commitments", return_value=items):
            markup, got = tb._checklist_markup()
        rows = markup["inline_keyboard"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0]["callback_data"], "done:3")
        self.assertIn("Reply to Rohan", rows[0][0]["text"])
        self.assertTrue(rows[0][0]["text"].startswith("⬜"))

    def test_empty_list_returns_none_markup(self):
        with patch("skills.commitment_manager.get_open_commitments", return_value=[]):
            markup, got = tb._checklist_markup()
        self.assertIsNone(markup)
        self.assertEqual(got, [])


class TestCallback(unittest.TestCase):
    def _cq(self, data="done:7"):
        return {"id": "cq1", "data": data,
                "message": {"chat": {"id": 999}, "message_id": 42}}

    def test_tap_completes_commitment_and_acks(self):
        with patch("skills.commitment_manager.complete", return_value="done") as comp, \
             patch.object(tb, "send_checklist") as render, \
             patch.object(tb, "_answer_callback") as ack:
            tb.handle_callback(self._cq("done:7"), allowed={"999"})
        comp.assert_called_once_with(7)                 # the tapped id
        ack.assert_called_once()
        self.assertIn("Done", ack.call_args[0][1])
        render.assert_called_once_with("999", message_id=42)   # re-render in place

    def test_unauthorized_chat_is_ignored(self):
        with patch("skills.commitment_manager.complete") as comp, \
             patch.object(tb, "_answer_callback") as ack:
            tb.handle_callback(self._cq(), allowed={"111"})    # 999 not allowed
        comp.assert_not_called()
        ack.assert_called_once()                        # still ack to clear the spinner

    def test_non_done_callback_just_acks(self):
        with patch("skills.commitment_manager.complete") as comp, \
             patch.object(tb, "_answer_callback") as ack, \
             patch.object(tb, "send_checklist") as render:
            tb.handle_callback(self._cq("noop:1"), allowed={"999"})
        comp.assert_not_called()
        render.assert_not_called()
        ack.assert_called_once()


if __name__ == "__main__":
    unittest.main()
