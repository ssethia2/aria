"""Tests for nightly compaction's durable-vs-ephemeral memory filter.

LLM and the ChromaDB store are mocked; these run offline.
Run: python3 -m unittest discover tests
"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import nightly_compaction


class TestCompactionFilter(unittest.TestCase):
    def setUp(self):
        fd, self.scratch = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        fd, self.convo = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.unlink(self.convo)                   # no conversation unless a test writes one
        self._c = patch.object(nightly_compaction, 'CONVERSATION_PATH', self.convo)
        self._c.start()
        self.llm = MagicMock()

    def tearDown(self):
        self._c.stop()
        for path in (self.scratch, self.convo):
            if os.path.exists(path):
                os.unlink(path)

    def _run(self, scratch_content, llm_output):
        with open(self.scratch, "w") as f:
            f.write(scratch_content)

        self.llm.invoke.return_value = MagicMock(content=llm_output)
        store = MagicMock(side_effect=lambda facts, added: len(facts))
        with patch.object(nightly_compaction, 'SCRATCHPAD_PATH', self.scratch), \
             patch.object(nightly_compaction, 'get_llm', return_value=self.llm), \
             patch.object(nightly_compaction, 'store_facts', store):
            nightly_compaction.compact_scratchpad()
        return store

    def test_conversations_are_compacted_then_wiped(self):
        # The itinerary he pasted in chat never reached memory; only add_memory did.
        with open(self.convo, "w") as f:
            f.write(json.dumps({"at": "2026-11-03T20:45", "user": "Flying home on TP 202, Lisbon 11:55 Nov 14",
                                "aria": "Got it."}) + "\n")
        fact = "Sam flies home on TP 202 from Lisbon at 11:55 on Nov 14, 2026."
        store = self._run("", fact)
        prompt = self.llm.invoke.call_args.args[0][1].content
        self.assertIn("User: Flying home on TP 202, Lisbon 11:55 Nov 14", prompt)
        self.assertEqual(self._stored(store), [fact])
        with open(self.convo) as f:
            self.assertEqual(f.read(), "")

    def _stored(self, store):
        return store.call_args.args[0] if store.called else []

    def test_durable_facts_are_stored_dated_and_scratchpad_wiped(self):
        store = self._run(
            "Satvik likes coffee.\n[Aria proactive action, 2026-06-10] Netflix handled.\n",
            "Satvik likes coffee.")
        self.assertEqual(self._stored(store), ["Satvik likes coffee."])
        noted = store.call_args.kwargs["added"]
        self.assertRegex(noted, r"^\d{4}-\d{2}-\d{2}$")   # dated by the scratchpad's last write
        with open(self.scratch) as f:
            self.assertEqual(f.read(), "")          # wiped

    def test_trailing_dates_and_numbers_survive(self):
        # The old cleanup stripped digits from BOTH ends ("…as of October 13, 2026." lost its year).
        store = self._run("notes\n", "1. Ashley's birthday is March 14\n- Rent went up to $2400\n"
                                      "* Backups stopped as of October 13, 2026.")
        self.assertEqual(self._stored(store), ["Ashley's birthday is March 14",
                                               "Rent went up to $2400",
                                               "Backups stopped as of October 13, 2026."])

    def test_nothing_response_stores_nothing_but_still_wipes(self):
        store = self._run(
            "[Aria proactive action, 2026-06-10] Netflix handled.\n"
            "[Aria proactive action, 2026-06-10] Reminder fired: stretch.\n",
            "NOTHING")
        self.assertEqual(self._stored(store), [])
        with open(self.scratch) as f:
            self.assertEqual(f.read(), "")          # operational lint still cleared

    def test_store_failure_keeps_the_scratchpad(self):
        with open(self.scratch, "w") as f:
            f.write("Satvik likes coffee.\n")
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="Satvik likes coffee.")
        with patch.object(nightly_compaction, 'SCRATCHPAD_PATH', self.scratch), \
             patch.object(nightly_compaction, 'get_llm', return_value=llm), \
             patch.object(nightly_compaction, 'store_facts',
                          side_effect=RuntimeError("ChromaDB is unavailable")):
            nightly_compaction.compact_scratchpad()
        with open(self.scratch) as f:
            self.assertIn("coffee", f.read())       # nothing lost when the store is down

    def test_empty_scratchpad_is_a_noop(self):
        store = self._run("", "irrelevant")
        store.assert_not_called()

    def test_no_cold_storage_tier(self):
        # ADR 0008: compaction never consolidates or deletes stored facts.
        self.assertFalse(hasattr(nightly_compaction, "consolidate_long_term_memory"))


if __name__ == '__main__':
    unittest.main()
