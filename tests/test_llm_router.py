"""Tests for the router's fallback-degradation alerting.

No network: the notify call and state path are patched.
Run: python3 -m unittest discover tests
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core import llm_router


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
        with patch('core.notify.send_telegram', return_value=True) as send:
            llm_router._notify_fallback_once("claude-opus-4-8", "404")
            llm_router._notify_fallback_once("claude-opus-4-8", "404 again")
        send.assert_called_once()
        self.assertIn("claude-opus-4-8", send.call_args[0][0])

    def test_different_models_alert_independently(self):
        with patch('core.notify.send_telegram', return_value=True) as send:
            llm_router._notify_fallback_once("claude-opus-4-8", "404")
            llm_router._notify_fallback_once("claude-haiku-4-5", "429")
        self.assertEqual(send.call_count, 2)

    def test_state_persists_across_calls(self):
        with patch('core.notify.send_telegram', return_value=True):
            llm_router._notify_fallback_once("claude-opus-4-8", "404")
        with open(self.state_path) as f:
            state = json.load(f)
        self.assertIn("claude-opus-4-8", state)

    def test_corrupt_state_file_still_alerts(self):
        with open(self.state_path, "w") as f:
            f.write("{not json")
        with patch('core.notify.send_telegram', return_value=True) as send:
            llm_router._notify_fallback_once("claude-opus-4-8", "404")
        send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
