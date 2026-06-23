"""LLM cost tracking: price math, recording/aggregation, and usage capture from a response.
Offline. Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cost_tracker as ct


class TestPricing(unittest.TestCase):
    def test_opus_input_output_rates(self):
        # 1M input @ $5, 1M output @ $25
        self.assertAlmostEqual(ct.estimate_cost("claude-opus-4-8", 1_000_000, 0), 5.0, places=4)
        self.assertAlmostEqual(ct.estimate_cost("claude-opus-4-8", 0, 1_000_000), 25.0, places=4)

    def test_gemini_free_tier_is_zero(self):
        self.assertEqual(ct.estimate_cost("gemini-2.5-flash", 500_000, 500_000), 0.0)

    def test_unknown_model_is_zero_not_crash(self):
        self.assertEqual(ct.estimate_cost("some-future-model", 1000, 1000), 0.0)

    def test_cache_read_is_cheap_creation_is_pricey(self):
        read = ct.estimate_cost("claude-opus-4-8", 0, 0, cache_read=1_000_000)
        creation = ct.estimate_cost("claude-opus-4-8", 0, 0, cache_creation=1_000_000)
        self.assertAlmostEqual(read, 5.0 * 0.1, places=4)      # 0.1x input
        self.assertAlmostEqual(creation, 5.0 * 2.0, places=4)  # 2x input (1h TTL)


class TestRecordAndSummary(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self._p = patch.object(ct, "LOG_PATH", os.path.join(self.d.name, "cost_log.json"))
        self._p.start()

    def tearDown(self):
        self._p.stop(); self.d.cleanup()

    def test_record_accumulates_and_summary_totals(self):
        ct.record("claude-opus-4-8", input_tokens=1000, output_tokens=2000)
        ct.record("claude-opus-4-8", input_tokens=1000, output_tokens=0)
        ct.record("gemini-2.5-flash", input_tokens=10_000, output_tokens=10_000)
        s = ct.summary(7)
        self.assertEqual(s["by_model"]["claude-opus-4-8"]["calls"], 2)
        self.assertEqual(s["by_model"]["gemini-2.5-flash"]["usd"], 0.0)   # free tier
        # opus: input 2000*$5/1M + output 2000*$25/1M = 0.01 + 0.05 = 0.06
        self.assertAlmostEqual(s["by_model"]["claude-opus-4-8"]["usd"], 0.06, places=4)
        self.assertAlmostEqual(s["total_usd"], 0.06, places=4)

    def test_record_never_raises(self):
        with patch.object(ct, "estimate_cost", side_effect=RuntimeError("boom")):
            ct.record("claude-opus-4-8", 1, 1)   # must swallow, not raise


class TestUsageCapture(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self._p = patch.object(ct, "LOG_PATH", os.path.join(self.d.name, "cost_log.json"))
        self._p.start()

    def tearDown(self):
        self._p.stop(); self.d.cleanup()

    def _result(self, usage, meta):
        msg = SimpleNamespace(usage_metadata=usage, response_metadata=meta)
        gen = SimpleNamespace(message=msg)
        return SimpleNamespace(generations=[[gen]], llm_output={})

    def test_handler_records_from_response(self):
        resp = self._result(
            {"input_tokens": 1000, "output_tokens": 500,
             "input_token_details": {"cache_read": 4000, "cache_creation": 0}},
            {"model": "claude-opus-4-8"})
        ct.CostTrackingHandler().on_llm_end(resp)
        s = ct.summary(1)
        m = s["by_model"]["claude-opus-4-8"]
        self.assertEqual(m["calls"], 1)
        self.assertEqual(m["cache_read"], 4000)

    def test_handler_missing_usage_is_noop(self):
        resp = self._result({}, {"model": "claude-opus-4-8"})
        ct.CostTrackingHandler().on_llm_end(resp)   # no usage → no crash, no record
        self.assertEqual(ct.summary(1)["by_model"], {})


if __name__ == "__main__":
    unittest.main()
