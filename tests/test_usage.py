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


class TestDollarCeiling(unittest.TestCase):
    def test_dollar_ceiling_blocks_even_under_count_cap(self):
        # Count cap is generous (100) but the $ ceiling is the binding constraint.
        with tempfile.TemporaryDirectory() as d:
            with patch.object(usage, "USAGE_PATH", Path(d) / "usage.json"), \
                 patch.dict(os.environ, {"ARIA_GUEST_DAILY_USD": "0.10",
                                         "ARIA_COST_AGENT": "0.06",
                                         "ARIA_GUEST_DAILY_AGENT": "100"}):
                self.assertTrue(usage.check_and_increment("al", "agent"))    # $0.06
                self.assertFalse(usage.check_and_increment("al", "agent"))   # $0.12 > $0.10

    def test_one_ceiling_across_mixed_actions(self):
        # voice + brain + chat all draw down the SAME daily $ pool.
        with tempfile.TemporaryDirectory() as d:
            with patch.object(usage, "USAGE_PATH", Path(d) / "usage.json"), \
                 patch.dict(os.environ, {"ARIA_GUEST_DAILY_USD": "0.20",
                                         "ARIA_COST_TOKENS": "0.15", "ARIA_COST_AGENT": "0.06",
                                         "ARIA_COST_QUICK": "0.002",
                                         "ARIA_GUEST_DAILY_TOKENS": "100",
                                         "ARIA_GUEST_DAILY_AGENT": "100"}):
                self.assertTrue(usage.check_and_increment("al", "tokens"))   # $0.15
                self.assertFalse(usage.check_and_increment("al", "agent"))   # 0.15+0.06 > 0.20
                self.assertTrue(usage.check_and_increment("al", "quick"))    # 0.15+0.002 <= 0.20

    def test_allow_never_charges_record_does(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(usage, "USAGE_PATH", Path(d) / "usage.json"), \
                 patch.dict(os.environ, {"ARIA_GUEST_DAILY_USD": "0.05", "ARIA_COST_QUICK": "0.05"}):
                self.assertTrue(usage.allow("al", "quick"))   # checking alone…
                self.assertTrue(usage.allow("al", "quick"))   # …never consumes budget
                usage.record("al", "quick")                   # now $0.05 spent
                self.assertFalse(usage.allow("al", "quick"))  # next would exceed $0.05


if __name__ == "__main__":
    unittest.main()
