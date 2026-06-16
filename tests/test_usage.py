"""Phase-4 cost caps: per-friend daily usage limits. Offline (temp usage file).

Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webvoice import usage


class TestDailyCaps(unittest.TestCase):
    def test_cap_blocks_after_limit_per_user_and_kind(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(usage, "USAGE_PATH", Path(d) / "usage.json"), \
                 patch.dict(os.environ, {"ARIA_GUEST_DAILY_TOKENS": "2"}):
                self.assertTrue(usage.check_and_increment("alice", "tokens"))   # 1
                self.assertTrue(usage.check_and_increment("alice", "tokens"))   # 2
                self.assertFalse(usage.check_and_increment("alice", "tokens"))  # 3 → over cap
                # a different friend has their own budget
                self.assertTrue(usage.check_and_increment("bob", "tokens"))
                # a different kind has its own budget (default agent cap is high)
                self.assertTrue(usage.check_and_increment("alice", "agent"))

    def test_counts_persist_across_calls(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(usage, "USAGE_PATH", Path(d) / "usage.json"), \
                 patch.dict(os.environ, {"ARIA_GUEST_DAILY_AGENT": "1"}):
                self.assertTrue(usage.check_and_increment("cara", "agent"))
                self.assertFalse(usage.check_and_increment("cara", "agent"))


if __name__ == "__main__":
    unittest.main()
