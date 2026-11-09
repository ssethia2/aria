"""Tests for the router's fallback-degradation alerting.

No network: the notify call and state path are patched.
Run: python3 -m unittest discover tests
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import llm_router


class TestFallbackAlertDedup(unittest.TestCase):
    def setUp(self):
        fd, self.state_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.state_path)
        self._patch = patch.object(llm_router, 'ALERT_STATE_PATH', self.state_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def test_alerts_once_per_model_per_day(self):
        with patch('notify.send_telegram', return_value=True) as send:
            llm_router._notify_fallback_once("claude-opus-4-8", "404")
            llm_router._notify_fallback_once("claude-opus-4-8", "404 again")
        send.assert_called_once()
        self.assertIn("claude-opus-4-8", send.call_args[0][0])

    def test_different_models_alert_independently(self):
        with patch('notify.send_telegram', return_value=True) as send:
            llm_router._notify_fallback_once("claude-opus-4-8", "404")
            llm_router._notify_fallback_once("claude-haiku-4-5", "429")
        self.assertEqual(send.call_count, 2)

    def test_state_persists_across_calls(self):
        with patch('notify.send_telegram', return_value=True):
            llm_router._notify_fallback_once("claude-opus-4-8", "404")
        with open(self.state_path) as f:
            state = json.load(f)
        self.assertIn("claude-opus-4-8", state)

    def test_corrupt_state_file_still_alerts(self):
        with open(self.state_path, "w") as f:
            f.write("{not json")
        with patch('notify.send_telegram', return_value=True) as send:
            llm_router._notify_fallback_once("claude-opus-4-8", "404")
        send.assert_called_once()


class TestLineup(unittest.TestCase):
    def test_tiers_lead_with_current_claude(self):
        self.assertEqual(llm_router.TIERS["light"][0][0], "claude-haiku-5-5")
        self.assertEqual(llm_router.TIERS["standard"][0][0], "claude-sonnet-5-5")
        self.assertEqual(llm_router.TIERS["heavy"][0][0], "claude-opus-5-5")

    def test_claude_models_skip_temperature_and_get_room_to_think(self):
        # The 5.5 models 400 on a non-default temperature, and with no LangChain profile
        # max_tokens would fall back to 4096, shared with thinking.
        for build in (llm_router._haiku, llm_router._sonnet, llm_router._opus):
            m = build(0)
            self.assertIsNone(m.temperature)
            self.assertEqual(m.max_tokens, 16000)
            self.assertEqual(m.thinking["block_binding"]["prefix_mismatch_behavior"],
                             "drop_block")
            self.assertIn("thinking-binding-controls-2026-08-01", m.betas)


class TestReplyText(unittest.TestCase):
    def test_skips_thinking_blocks(self):
        msg = type("M", (), {"content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": '[{"id": 1}]'}]})()
        self.assertEqual(llm_router.reply_text(msg), '[{"id": 1}]')

    def test_plain_string_and_gemini_string_lists(self):
        self.assertEqual(llm_router.reply_text(type("M", (), {"content": "hi"})()), "hi")
        self.assertEqual(llm_router.reply_text(["a", "b"]), "ab")

    def test_no_text_is_empty_not_a_repr(self):
        self.assertEqual(llm_router.reply_text([{"type": "thinking", "thinking": ""}]), "")


if __name__ == '__main__':
    unittest.main()
