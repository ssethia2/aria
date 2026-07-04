"""Tests for the commitment pattern miner (commitment_patterns / analyze_commitments).

Seeds a temp DB and asserts the deterministic patterns. Offline.
Run: python3 -m unittest discover tests
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from skills import commitment_manager as cm


class TestCommitmentPatterns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._patch = patch.object(cm, "DB_PATH", self.tmp.name)
        self._patch.start()
        cm.init_db()

    def tearDown(self):
        self._patch.stop()
        os.unlink(self.tmp.name)

    def _add(self, desc, kind, who, due, status, completed=None):
        conn = sqlite3.connect(self.tmp.name)
        conn.execute(
            "INSERT INTO commitments (description, kind, who, due_date, status, source, "
            "created_at, completed_at) VALUES (?,?,?,?,?,'chat','2026-06-01 09:00:00',?)",
            (desc, kind, who, due, status, completed))
        conn.commit(); conn.close()

    def test_detects_overdue_concentration_and_late(self):
        today = date(2026, 6, 16)
        self._add("Reply to Diane re trip", "reply_owed", "Diane", "2026-06-10", "open")
        self._add("Reply to Diane re photos", "reply_owed", "Diane", "2026-06-12", "open")
        self._add("Ashley birthday", "people_date", "Ashley", "2026-08-04", "open")  # future
        self._add("File taxes", "deadline", None, "2026-06-05", "done", "2026-06-09 10:00:00")  # late
        for i in range(4):  # enough done-with-due for the late-rate finding
            self._add(f"done{i}", "deadline", None, "2026-06-08", "done", "2026-06-08 10:00:00")

        p = cm.commitment_patterns(today=today)
        self.assertEqual(len(p["overdue"]), 2)                 # the two Diane items
        self.assertEqual(p["overdue"][0]["days_overdue"], 6)   # oldest first
        self.assertEqual(p["overdue_by_who"].get("Diane"), 2)
        self.assertEqual(p["late_completions"], 1)
        self.assertEqual(p["done_with_due"], 5)
        joined = " ".join(p["findings"]).lower()
        self.assertIn("overdue", joined)
        self.assertIn("diane", joined)

    def test_clean_slate_has_no_findings(self):
        # "future thing" must be relative — a hardcoded date silently becomes overdue when
        # the real calendar passes it (analyze_commitments below runs with the REAL today).
        from datetime import timedelta
        future = (date.today() + timedelta(days=30)).isoformat()
        self._add("done on time", "deadline", None, "2026-06-08", "done", "2026-06-08 09:00:00")
        self._add("future thing", "promise", None, future, "open")
        p = cm.commitment_patterns(today=date(2026, 6, 16))
        self.assertEqual(p["findings"], [])
        self.assertIn("nothing notable", cm.analyze_commitments.invoke({}).lower())


if __name__ == "__main__":
    unittest.main()
