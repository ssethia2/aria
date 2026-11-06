"""Secrets never reach the logs (redact.py). Offline.
Run: python3 -m unittest discover tests
"""
import io
import os
import sys
import unittest
from unittest.mock import patch

import redact

FAKE = "123456789:AAFakeTokenForTestsOnly_xyz"


class TestRedact(unittest.TestCase):
    def test_printed_errors_lose_the_token(self):
        # A poll error's URL carried the bot token into the log on every network blip.
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": FAKE}), \
             patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            redact.install()
            print(f"[poll error] Max retries exceeded with url: /bot{FAKE}/getUpdates")
            sys.stderr.write(f"Traceback ... /bot{FAKE}/sendMessage\n")
            redact.install()                     # idempotent: no double wrapping
            self.assertIsInstance(sys.stdout, redact._Redacting)
            self.assertNotIsInstance(sys.stdout._stream, redact._Redacting)
        self.assertNotIn(FAKE, out.getvalue() + err.getvalue())
        self.assertIn("/bot<redacted>/getUpdates", out.getvalue())

    def test_nothing_to_hide_leaves_streams_alone(self):
        out = io.StringIO()
        with patch.dict(os.environ, {n: "" for n in redact.SECRET_NAMES}), \
             patch.object(sys, "stdout", out):
            redact.install()
            self.assertIs(sys.stdout, out)


if __name__ == "__main__":
    unittest.main()
