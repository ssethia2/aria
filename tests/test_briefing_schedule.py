"""Tests for when the briefing runs (morning_run.py) and how it greets.

The schedule is BRIEFING_TIME in BRIEFING_TZ, read by Python rather than launchd, so it
holds when the laptop changes timezone. Offline: the briefing itself is mocked.
Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import healthcheck as hc
import morning_run

NY = ZoneInfo("America/New_York")
DUBAI = ZoneInfo("Asia/Dubai")
PINNED = {"BRIEFING_TZ": "America/New_York", "BRIEFING_TIME": "08:00"}


class StateMixin:
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self._p = patch.object(morning_run, "STATE_PATH", self.path)
        self._p.start()
        self._env = patch.dict(os.environ, PINNED)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._p.stop()
        if os.path.exists(self.path):
            os.unlink(self.path)


class TestSchedule(StateMixin, unittest.TestCase):
    def test_slot_is_in_the_pinned_zone_wherever_the_laptop_is(self):
        # 8:00 AM New York is 5:00 PM in Dubai.
        self.assertIsNone(morning_run.due(datetime(2026, 11, 3, 16, 55, tzinfo=DUBAI)))
        at = morning_run.due(datetime(2026, 11, 3, 17, 0, tzinfo=DUBAI))
        self.assertEqual(at, datetime(2026, 11, 3, 8, 0, tzinfo=NY))

    def test_once_per_slot(self):
        now = datetime(2026, 11, 3, 8, 15, tzinfo=NY)
        morning_run._save_state({"slot": "2026-11-03"})
        self.assertIsNone(morning_run.due(now))
        self.assertIsNotNone(morning_run.due(now + timedelta(days=1)))

    def test_a_briefing_half_a_day_late_is_skipped(self):
        self.assertIsNotNone(morning_run.due(datetime(2026, 11, 3, 19, 0, tzinfo=NY)))
        self.assertIsNone(morning_run.due(datetime(2026, 11, 3, 20, 30, tzinfo=NY)))

    def test_unset_zone_means_host_time(self):
        with patch.dict(os.environ, {"BRIEFING_TZ": ""}):
            at = morning_run.scheduled_for(datetime(2026, 11, 3, 9, 0).astimezone())
        self.assertEqual((at.hour, at.minute), (8, 0))
        self.assertEqual(at.utcoffset(), datetime(2026, 11, 3, 9, 0).astimezone().utcoffset())

    def test_run_records_the_slot_first_and_success_after(self):
        seen = {}
        with patch.object(morning_run, "due", return_value=datetime(2026, 11, 3, 8, 0, tzinfo=NY)), \
             patch.object(morning_run, "run", side_effect=lambda: seen.update(morning_run.load_state())), \
             patch("dotenv.load_dotenv"):
            morning_run.main()
        self.assertEqual(seen, {"slot": "2026-11-03"})          # marked before running
        self.assertIn("last_ok_ts", morning_run.load_state())

    def test_a_failed_run_is_not_retried_every_15_minutes(self):
        with patch.object(morning_run, "due", return_value=datetime(2026, 11, 3, 8, 0, tzinfo=NY)), \
             patch.object(morning_run, "run", side_effect=RuntimeError("gmail down")), \
             patch("dotenv.load_dotenv"):
            with self.assertRaises(RuntimeError):
                morning_run.main()
        self.assertEqual(morning_run.load_state(), {"slot": "2026-11-03"})
        self.assertIsNone(morning_run.due(datetime(2026, 11, 3, 8, 30, tzinfo=NY)))


class TestHealthcheck(StateMixin, unittest.TestCase):
    def _check(self, now):
        with patch.object(hc, "datetime") as dt:
            dt.now.return_value = now
            return hc.check_briefing_today()

    def test_not_due_in_the_first_hour(self):
        status, _ = self._check(datetime(2026, 11, 3, 8, 30, tzinfo=NY))
        self.assertEqual(status, hc.OK)

    def test_ok_after_a_successful_run_warn_without(self):
        now = datetime(2026, 11, 3, 10, 0, tzinfo=NY)
        morning_run._save_state({"slot": "2026-11-03",
                                 "last_ok_ts": datetime(2026, 11, 2, 8, 5, tzinfo=NY).timestamp()})
        self.assertEqual(self._check(now)[0], hc.WARN)           # yesterday's doesn't count
        morning_run._save_state({"slot": "2026-11-03",
                                 "last_ok_ts": datetime(2026, 11, 3, 8, 5, tzinfo=NY).timestamp()})
        self.assertEqual(self._check(now)[0], hc.OK)


class TestGreeting(unittest.TestCase):
    def test_greets_by_his_clock(self):
        from main import greeting
        self.assertIn("morning", greeting(datetime(2026, 11, 3, 8, 0)))
        self.assertIn("afternoon", greeting(datetime(2026, 11, 3, 13, 0)))
        self.assertIn("evening", greeting(datetime(2026, 11, 3, 18, 30)))


if __name__ == "__main__":
    unittest.main()
