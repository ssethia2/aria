"""Tests for the uncapped, dated semantic store (ADR 0008) and the cold-storage import.

Runs against a real in-memory ChromaDB with fake embeddings; no network.
Run: python3 -m unittest discover tests
"""
import datetime
import hashlib
import os
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

import chromadb
from chromadb.config import Settings

import import_cold_storage
import memory


def _vec(text):
    """Deterministic 16-dim fake embedding; identical text -> identical vector."""
    h = hashlib.sha256(text.lower().encode()).digest()
    return [b / 255 for b in h[:16]]


class StoreCase(unittest.TestCase):
    def setUp(self):
        client = chromadb.EphemeralClient(Settings(anonymized_telemetry=False))
        self.col = client.create_collection(f"t_{uuid.uuid4().hex}")
        emb = MagicMock()
        emb.embed_query.side_effect = _vec
        emb.embed_documents.side_effect = lambda texts, task_type=None: [_vec(t) for t in texts]
        fd, missing = tempfile.mkstemp()
        os.close(fd)
        os.unlink(missing)                      # a scratchpad path that doesn't exist
        self.patches = [patch.object(memory, "collection", self.col),
                        patch.object(memory, "embeddings", emb),
                        patch.object(memory, "SCRATCHPAD_PATH", missing)]
        for p in self.patches:
            p.start()
        self.emb = emb

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()


class TestStoreFacts(StoreCase):
    def test_stores_with_date_and_source(self):
        n = memory.store_facts(["Satvik likes espresso."], added="2026-10-14")
        self.assertEqual(n, 1)
        got = self.col.get(include=["documents", "metadatas"])
        self.assertEqual(got["documents"], ["Satvik likes espresso."])
        self.assertEqual(got["metadatas"][0], {"added": "2026-10-14", "source": "compaction"})

    def test_skips_exact_duplicates_ignoring_case_and_spacing(self):
        memory.store_facts(["Satvik likes espresso."], added="2026-10-01")
        n = memory.store_facts(["satvik  likes ESPRESSO.", "Satvik runs on Sundays.", ""],
                               added="2026-10-14")
        self.assertEqual(n, 1)
        self.assertEqual(self.col.count(), 2)

    def test_unavailable_store_raises(self):
        with patch.object(memory, "collection", None):
            with self.assertRaises(RuntimeError):
                memory.store_facts(["x"], added="2026-10-14")

    def test_waits_out_the_embedding_quota(self):
        calls = {"n": 0}

        def quota_once(texts, task_type=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED: embed_content_free_tier_requests")
            return [_vec(t) for t in texts]
        self.emb.embed_documents.side_effect = quota_once
        with patch.object(memory.time, "sleep") as sleep:
            n = memory.store_facts(["Satvik likes espresso."], added="2026-10-14")
        self.assertEqual(n, 1)
        sleep.assert_called_once()

    def test_other_errors_raise_and_store_nothing(self):
        self.emb.embed_documents.side_effect = RuntimeError("bad key")
        with self.assertRaises(RuntimeError):
            memory.store_facts(["a.", "b."], added="2026-10-14")
        self.assertEqual(self.col.count(), 0)

    def test_matches_how_existing_vectors_were_made(self):
        memory.store_facts(["Satvik likes espresso."], added="2026-10-14")
        self.assertEqual(self.emb.embed_documents.call_args.kwargs["task_type"], "RETRIEVAL_QUERY")

    def test_no_cap(self):
        memory.store_facts([f"Fact number {i}." for i in range(150)], added="2026-10-14")
        self.assertEqual(self.col.count(), 150)   # nothing consolidated away at 100


class TestSearchShowsDates(StoreCase):
    def test_dated_and_undated_results(self):
        memory.store_facts(["Satvik likes espresso."], added="2026-10-14")
        self.col.add(ids=["old"], documents=["Satvik likes tea."], embeddings=[_vec("tea")])
        out = memory._search_memory("drinks")
        self.assertIn("- Satvik likes espresso. (noted 2026-10-14)", out)
        self.assertIn("- Satvik likes tea.", out)
        self.assertNotIn("tea. (noted", out)

    def test_returns_five_by_default(self):
        memory.store_facts([f"Fact number {i}." for i in range(8)], added="2026-10-14")
        out = memory._search_memory("fact")
        self.assertEqual(out.count("(noted 2026-10-14)"), 5)


class TestRecentFacts(StoreCase):
    def test_window_and_order(self):
        memory.store_facts(["Older fact."], added="2026-10-01")
        memory.store_facts(["Monday fact."], added="2026-10-12")
        memory.store_facts(["Thursday fact."], added="2026-10-15")
        self.col.add(ids=["u"], documents=["Undated fact."], embeddings=[_vec("u")])
        got = memory.recent_facts(days=7, today=datetime.date(2026, 10, 18))
        self.assertEqual(got, ["Monday fact.", "Thursday fact."])


class TestImportColdStorage(StoreCase):
    POINTER = ("Extensive long-term memory archive created on 20261010_080141. Contains deeply "
               "consolidated facts. To access this information, use the read_cold_storage tool "
               "with the filename: memory_archive_20261010_080141.txt")

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        for name, body in (("memory_archive_20260620_080122.txt", "June narrative"),
                           ("memory_archive_20261010_080141.txt", "October narrative")):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write(body)
        self.col.add(ids=["ptr"], documents=[self.POINTER], embeddings=[_vec(self.POINTER)])
        self.col.add(ids=["keep"], documents=["The user's iCloud storage is full."],
                     embeddings=[_vec("icloud")])
        self.llm = MagicMock()
        self.llm.invoke.side_effect = lambda msgs: MagicMock(content=(
            "# IDENTITY\n- Satvik prefers tea.\n2. Ashley's birthday is March 14"
            if "June" in msgs[1].content else
            "Satvik switched to coffee in August 2026.\nRohan hikes on Saturdays."))
        self.dir_patch = patch.object(import_cold_storage, "COLD_STORAGE_DIR", self.dir)
        self.llm_patch = patch.object(import_cold_storage, "get_llm", return_value=self.llm)
        self.dir_patch.start()
        self.llm_patch.start()

    def tearDown(self):
        self.llm_patch.stop()
        self.dir_patch.stop()
        super().tearDown()

    def _docs(self):
        got = self.col.get(include=["documents", "metadatas"])
        return {d: m for d, m in zip(got["documents"], got["metadatas"])}

    def test_imports_dated_facts_and_removes_pointers(self):
        self.assertEqual(import_cold_storage.main([]), 0)
        docs = self._docs()
        self.assertEqual(docs["Ashley's birthday is March 14"],
                         {"added": "2026-06-20",
                          "source": "archive:memory_archive_20260620_080122.txt"})
        self.assertEqual(docs["Rohan hikes on Saturdays."]["added"], "2026-10-10")
        self.assertNotIn(self.POINTER, docs)                        # pointer gone
        self.assertIn("The user's iCloud storage is full.", docs)   # existing facts untouched
        self.assertNotIn("IDENTITY", " ".join(docs))                # headings skipped
        self.assertEqual(len(docs), 5)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "memory_archive_20260620_080122.txt")))

    def test_rerun_is_a_noop(self):
        import_cold_storage.main([])
        calls = self.llm.invoke.call_count
        import_cold_storage.main([])
        self.assertEqual(self.llm.invoke.call_count, calls)       # no re-split
        self.assertEqual(self.col.count(), 5)

    def test_dry_run_changes_nothing(self):
        import_cold_storage.main(["--dry-run"])
        self.assertEqual(self.col.count(), 2)
        self.assertIn(self.POINTER, self._docs())

    def test_failure_rolls_back_that_archive_and_keeps_pointers(self):
        calls = {"n": 0}

        def flaky(texts, task_type=None):
            calls["n"] += 1
            if calls["n"] == 2:                  # the October archive's batch fails
                raise RuntimeError("embedding API down")
            return [_vec(t) for t in texts]
        self.emb.embed_documents.side_effect = flaky
        with self.assertRaises(RuntimeError):
            import_cold_storage.main([])
        sources = {(m or {}).get("source") for m in self._docs().values()}
        self.assertIn("archive:memory_archive_20260620_080122.txt", sources)
        self.assertNotIn("archive:memory_archive_20261010_080141.txt", sources)
        self.assertIn(self.POINTER, self._docs())                   # pointers kept until done
        self.emb.embed_documents.side_effect = lambda texts, task_type=None: [_vec(t) for t in texts]
        import_cold_storage.main([])                                # rerun finishes the job
        self.assertEqual(self.col.count(), 5)
        self.assertNotIn(self.POINTER, self._docs())


if __name__ == "__main__":
    unittest.main()
