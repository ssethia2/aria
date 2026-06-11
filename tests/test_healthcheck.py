"""Tests for the self-diagnosis system.

Run: python3 -m unittest discover tests
"""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import healthcheck as hc


class TestStatusLogic(unittest.TestCase):
    def test_worst_escalates(self):
        self.assertEqual(hc.worst([('a', hc.OK, ''), ('b', hc.OK, '')]), hc.OK)
        self.assertEqual(hc.worst([('a', hc.OK, ''), ('b', hc.WARN, '')]), hc.WARN)
        self.assertEqual(hc.worst([('a', hc.WARN, ''), ('b', hc.FAIL, '')]), hc.FAIL)

    def test_summary_headlines_by_worst(self):
        self.assertIn("healthy", hc.summary([('x', hc.OK, 'fine')]).lower())
        self.assertIn("attention", hc.summary([('x', hc.FAIL, 'broke')]).lower())

    def test_broken_check_becomes_fail_not_crash(self):
        def boom():
            raise RuntimeError("kaboom")
        name, status, detail = hc._check("x", boom)
        self.assertEqual(status, hc.FAIL)
        self.assertIn("kaboom", detail)


class TestSecrets(unittest.TestCase):
    def test_missing_core_secret_fails(self):
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': '', 'ANTHROPIC_API_KEY': 'x'}, clear=False):
            status, _ = hc.check_secrets()
        self.assertEqual(status, hc.FAIL)

    def test_all_present_ok(self):
        env = {'TELEGRAM_BOT_TOKEN': 't', 'ANTHROPIC_API_KEY': 'a',
               'GEMINI_API_KEY': 'g', 'TELEGRAM_ALLOWED_CHAT_ID': '1'}
        with patch.dict('os.environ', env, clear=False):
            status, _ = hc.check_secrets()
        self.assertEqual(status, hc.OK)


class TestEngineFreshness(unittest.TestCase):
    def _with_state(self, state):
        d = tempfile.mkdtemp()
        path = os.path.join(d, 'engine_state.json')
        if state is not None:
            with open(path, 'w') as f:
                json.dump(state, f)
        return patch.object(hc, '_p', lambda *a: path if a == ('engine_state.json',) else os.path.join(d, *a))

    def test_fresh_tick_ok(self):
        with self._with_state({'email-digest': {'last_check_ts': time.time() - 120}}):
            status, _ = hc.check_engine_freshness()
        self.assertEqual(status, hc.OK)

    def test_stale_tick_fails(self):
        with self._with_state({'email-digest': {'last_check_ts': time.time() - 7200}}):
            status, detail = hc.check_engine_freshness()
        self.assertEqual(status, hc.FAIL)
        self.assertIn("running", detail)

    def test_no_state_warns(self):
        with self._with_state(None):
            status, _ = hc.check_engine_freshness()
        self.assertEqual(status, hc.WARN)


if __name__ == '__main__':
    unittest.main()
