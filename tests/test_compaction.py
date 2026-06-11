"""Tests for nightly compaction's durable-vs-ephemeral memory filter.

LLM, ChromaDB, and the Tier-3 trigger are mocked; these run offline.
Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import nightly_compaction


class TestCompactionFilter(unittest.TestCase):
    def setUp(self):
        fd, self.scratch = tempfile.mkstemp(suffix=".txt")
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.scratch):
            os.unlink(self.scratch)

    def _run(self, scratch_content, llm_output):
        with open(self.scratch, "w") as f:
            f.write(scratch_content)

        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=llm_output)
        collection = MagicMock()
        embeddings = MagicMock()
        embeddings.embed_query.return_value = [0.0]

        with patch.object(nightly_compaction, 'SCRATCHPAD_PATH', self.scratch), \
             patch.object(nightly_compaction, 'get_llm', return_value=llm), \
             patch.object(nightly_compaction, 'collection', collection), \
             patch.object(nightly_compaction, 'embeddings', embeddings), \
             patch.object(nightly_compaction, 'consolidate_long_term_memory') as tier3:
            nightly_compaction.compact_scratchpad()
        return collection, tier3

    def test_durable_facts_are_embedded_and_scratchpad_wiped(self):
        collection, tier3 = self._run(
            "Satvik likes coffee.\n[Aria proactive action, 2026-06-10] Netflix handled.\n",
            "Satvik likes coffee.")
        self.assertEqual(collection.add.call_count, 1)
        self.assertIn("coffee", collection.add.call_args.kwargs['documents'][0])
        with open(self.scratch) as f:
            self.assertEqual(f.read(), "")          # wiped
        tier3.assert_called_once()                   # Tier-3 trigger actually runs now

    def test_nothing_response_embeds_no_vectors_but_still_wipes(self):
        collection, _ = self._run(
            "[Aria proactive action, 2026-06-10] Netflix handled.\n"
            "[Aria proactive action, 2026-06-10] Reminder fired: stretch.\n",
            "NOTHING")
        collection.add.assert_not_called()
        with open(self.scratch) as f:
            self.assertEqual(f.read(), "")          # operational lint still cleared

    def test_empty_scratchpad_is_a_noop(self):
        collection, tier3 = self._run("", "irrelevant")
        collection.add.assert_not_called()
        tier3.assert_not_called()


if __name__ == '__main__':
    unittest.main()
