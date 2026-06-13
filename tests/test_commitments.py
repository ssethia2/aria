"""Tests for the commitment store — capture, due logic, recurrence, migration.

Uses a throwaway SQLite file; no network.
Run: python3 -m unittest discover tests
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from skills import commitment_manager as cm


class TempDBMixin:
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        self._patch = patch.object(cm, 'DB_PATH', self.db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        if os.path.exists(self.db):
            os.unlink(self.db)


class TestStore(TempDBMixin, unittest.TestCase):
    def test_add_and_open_ordering_undated_last(self):
        cm.add("undated promise")
        cm.add("due soon", due_date="2026-06-11")
        cm.add("due later", due_date="2026-06-20")
        descs = [c['description'] for c in cm.get_open_commitments()]
        self.assertEqual(descs, ["due soon", "due later", "undated promise"])

    def test_due_includes_overdue_excludes_future(self):
        cm.add("overdue", due_date="2026-06-01")
        cm.add("today", due_date="2026-06-10")
        cm.add("future", due_date="2026-06-15")
        cm.add("undated")
        due = [c['description'] for c in cm.get_due_commitments(today="2026-06-10")]
        self.assertEqual(due, ["overdue", "today"])

    def test_upcoming_window(self):
        cm.add("tomorrow", due_date="2026-06-11")
        cm.add("next month", due_date="2026-07-20")
        up = [c['description'] for c in cm.get_upcoming_commitments(days=7, today="2026-06-10")]
        self.assertEqual(up, ["tomorrow"])

    def test_timed_due_now_gates_on_time(self):
        cm.add("call at 3", due_date="2026-06-10", due_time="15:00")
        cm.add("call at 5", due_date="2026-06-10", due_time="17:00")
        cm.add("dateless timed-less")
        now = datetime(2026, 6, 10, 15, 1)
        due = [c['description'] for c in cm.get_timed_due_now(now)]
        self.assertEqual(due, ["call at 3"])

    def test_complete_closes_normal(self):
        cid = cm.add("one-off", due_date="2026-06-10")
        msg = cm.complete(cid)
        self.assertIn("done", msg)
        self.assertEqual(cm.get_open_commitments(), [])

    def test_complete_yearly_rolls_to_next_year(self):
        cid = cm.add("Mom's birthday", kind='people_date',
                     due_date="2026-07-03", recurring='yearly')
        msg = cm.complete(cid)
        self.assertIn("2027-07-03", msg)
        open_ = cm.get_open_commitments()
        self.assertEqual(len(open_), 1)
        self.assertEqual(open_[0]['due_date'], "2027-07-03")

    def test_next_date_per_recurrence(self):
        self.assertEqual(cm._next_date("2026-06-12", "daily"), "2026-06-13")
        self.assertEqual(cm._next_date("2026-06-12", "weekly"), "2026-06-19")
        self.assertEqual(cm._next_date("2026-06-12", "monthly"), "2026-07-12")
        self.assertEqual(cm._next_date("2026-12-31", "monthly"), "2027-01-31")
        self.assertEqual(cm._next_date("2026-06-12", "yearly"), "2027-06-12")
        self.assertEqual(cm._next_date("2026-06-12", "every_3_days"), "2026-06-15")

    def test_normalize_recurrence(self):
        self.assertEqual(cm.normalize_recurrence("Weekly"), "weekly")
        self.assertEqual(cm.normalize_recurrence("every 3 days"), "every_3_days")
        self.assertIsNone(cm.normalize_recurrence("sometimes"))

    def test_advance_recurring_skips_missed_periods_in_one_jump(self):
        # Weekly reminder due 3 weeks ago + bot offline → roll to next future, once.
        cid = cm.add("weekly check-in", due_date="2026-05-22", recurring="weekly")
        cm.advance_recurring(cid, today="2026-06-12")
        due = cm.get_open_commitments()[0]['due_date']
        self.assertGreater(due, "2026-06-12")          # strictly future
        self.assertEqual(due, "2026-06-19")             # next weekly slot

    def test_pingable_includes_recurring_after_default_hour(self):
        cm.add("daily standup nudge", due_date="2026-06-12", recurring="daily")
        morning = cm.get_pingable_now(datetime(2026, 6, 12, 10, 0))
        self.assertEqual(len(morning), 1)
        self.assertTrue(morning[0]['is_recurring'])
        early = cm.get_pingable_now(datetime(2026, 6, 12, 6, 0))  # before 9am
        self.assertEqual(early, [])

    def test_recurring_complete_rolls_forward(self):
        cid = cm.add("water plants", due_date="2026-06-12", recurring="every_3_days")
        msg = cm.complete(cid)
        self.assertIn("2026-06-15", msg)
        self.assertEqual(cm.get_open_commitments()[0]['due_date'], "2026-06-15")

    def test_recurring_excluded_from_due_and_never_overdue(self):
        cm.add("HubSpot check-in", due_date="2026-06-01", recurring="weekly")  # stored date is past
        cm.add("one-off", due_date="2026-06-01")
        due = [c['description'] for c in cm.get_due_commitments(today="2026-06-13")]
        self.assertIn("one-off", due)
        self.assertNotIn("HubSpot check-in", due)             # recurring not in due/overdue
        rec = [c for c in cm.get_open_commitments() if c['recurring']][0]
        line = cm.format_line(rec, today="2026-06-13")
        self.assertNotIn("OVERDUE", line)
        self.assertIn("repeats weekly", line)
        self.assertIn("next 2026-06", line)                   # shows a future occurrence

    def test_has_open_reply_owed_dedupes_thread(self):
        cm.add("Reply to Diane: Project X", kind="reply_owed", who="Diane", due_date="2026-06-13")
        self.assertTrue(cm.has_open_reply_owed("Diane", "Re: Project X"))      # Re: variant
        self.assertTrue(cm.has_open_reply_owed("diane", "project x"))          # case-insensitive
        self.assertFalse(cm.has_open_reply_owed("Diane", "Different topic"))
        self.assertFalse(cm.has_open_reply_owed("Bob", "Project X"))           # different person

    def test_resolve_replied_completes_answered_threads(self):
        a = cm.add("Reply to Diane: Project X", kind="reply_owed", who="Diane", due_date="2026-06-13")
        cm.add("Reply to Bob: Lunch?", kind="reply_owed", who="Bob", due_date="2026-06-13")
        # checker says the user replied only to the Diane thread
        resolved = cm.resolve_replied(lambda subj, since: "Project X" in subj)
        self.assertEqual(resolved, ["Reply to Diane: Project X"])
        open_descs = [c['description'] for c in cm.get_open_commitments()]
        self.assertNotIn("Reply to Diane: Project X", open_descs)
        self.assertIn("Reply to Bob: Lunch?", open_descs)

    def test_drop_removes_from_open(self):
        cid = cm.add("never mind")
        self.assertTrue(cm.drop(cid))
        self.assertEqual(cm.get_open_commitments(), [])

    def test_format_line_marks_overdue(self):
        cid = cm.add("renew passport", due_date="2026-06-01")
        c = cm.get_open_commitments()[0]
        line = cm.format_line(c, today="2026-06-10")
        self.assertIn("OVERDUE", line)
        self.assertIn(f"#{cid}", line)


class TestLegacyMigration(TempDBMixin, unittest.TestCase):
    def _make_legacy(self):
        conn = sqlite3.connect(self.db)
        conn.execute('''CREATE TABLE reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL,
            target_date TEXT NOT NULL, created_at TEXT NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT 0)''')
        conn.execute("INSERT INTO reminders (task, target_date, created_at, completed) "
                     "VALUES ('old open', '2026-06-12', '2026-06-01 10:00:00', 0)")
        conn.execute("INSERT INTO reminders (task, target_date, created_at, completed) "
                     "VALUES ('old done', '2026-06-01', '2026-06-01 10:00:00', 1)")
        conn.commit()
        conn.close()

    def test_open_reminders_migrate_once(self):
        self._make_legacy()
        open_ = cm.get_open_commitments()   # triggers init + migration
        self.assertEqual(len(open_), 1)
        self.assertEqual(open_[0]['description'], 'old open')
        self.assertEqual(open_[0]['source'], 'migrated')
        cm.init_db()                         # idempotent — no duplicates
        self.assertEqual(len(cm.get_open_commitments()), 1)


if __name__ == '__main__':
    unittest.main()
