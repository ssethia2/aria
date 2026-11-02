"""Tests for moments.py — reading the calendar, timing touchpoints, and the legwork.

Offline: calendar items are hand-built dicts; Gmail, commitments and people are mocked.
Run: python3 -m unittest discover tests
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import moments
from moments import Moment, TOUCHPOINTS

LOCAL = ZoneInfo("Asia/Dubai")
ME = "Sam sammercer@example.com"


def _ev(summary, start, end=None, **extra):
    ev = {"id": summary.replace(" ", "")[:12], "summary": summary,
          "start": {"dateTime": start, "timeZone": "America/New_York"},
          "end": {"dateTime": end or start}}
    ev.update(extra)
    return ev


def _meeting(start, minutes=30, **kw):
    return Moment(key="k", kind="meeting", title=kw.pop("title", "Interview with Acme"),
                  start=start, end=start and start + timedelta(minutes=minutes), **kw)


def _due(m, now):
    return [tp.name for tp in moments.due_touchpoints(m, now)]


class TestFromEvents(unittest.TestCase):
    def test_kinds_and_skips(self):
        items = [
            _ev("Interview with Acme", "2026-11-05T13:00:00-05:00", "2026-11-05T13:30:00-05:00"),
            _ev("Flight to Lisbon (TP 202)", "2026-11-06T11:55:00-05:00", eventType="fromGmail"),
            _ev("Reservation at Lily Cafe", "2026-11-07T19:00:00-05:00", eventType="fromGmail"),
            {"id": "bday", "summary": "Kim birthday", "start": {"date": "2026-11-05"},
             "end": {"date": "2026-11-06"}},                                   # all-day
            _ev("Old sync", "2026-11-05T09:00:00-05:00", status="cancelled"),
            _ev("Declined call", "2026-11-05T10:00:00-05:00",
                attendees=[{"email": "sammercer@example.com", "self": True,
                            "responseStatus": "declined"}]),
        ]
        got = [(m.kind, m.title) for m in moments.from_events(items, ME)]
        self.assertEqual(got, [("meeting", "Interview with Acme"),
                               ("flight", "Flight to Lisbon (TP 202)"),
                               ("booking", "Reservation at Lily Cafe")])

    def test_people_exclude_him_rooms_and_group_calendars(self):
        ev = _ev("Acme intro", "2026-11-05T13:00:00-05:00", attendees=[
            {"email": "sammercer@example.com", "self": True},
            {"email": "kim@acme.com", "displayName": "Kim Park"},
            {"email": "room-4@resource.example.com", "resource": True},
            {"email": "c_abc123@group.calendar.google.com"}],
            organizer={"email": "kim@acme.com"},
            hangoutLink="https://meet.google.com/abc-defg-hij")
        m = moments.from_events([ev], ME)[0]
        self.assertEqual(m.people, [{"name": "Kim Park", "email": "kim@acme.com"}])
        self.assertEqual(m.link, "https://meet.google.com/abc-defg-hij")
        self.assertEqual(m.event_tz, "America/New_York")

    def test_two_invites_for_one_interview_merge(self):
        # The company's scheduler and the interviewer each send one, same time.
        items = [
            _ev("Interview with Acme", "2026-11-10T13:00:00-05:00"),
            _ev("Sam Mercer / Kim Park Acme", "2026-11-10T13:00:00-05:00",
                attendees=[{"email": "kim@acme.com", "displayName": "Kim Park"}],
                hangoutLink="https://meet.google.com/abc-defg-hij"),
            _ev("Dentist", "2026-11-10T13:00:00-05:00"),     # same time, unrelated: a conflict
        ]
        ms = moments.from_events(items, ME)
        self.assertEqual(len(ms), 2)
        acme = next(m for m in ms if "Acme" in m.title)
        self.assertEqual([p["email"] for p in acme.people], ["kim@acme.com"])
        self.assertEqual(acme.link, "https://meet.google.com/abc-defg-hij")


class TestDistinctiveWords(unittest.TestCase):
    def test_generic_words_and_his_own_name_drop_out(self):
        self.assertEqual(moments.distinctive_words("Interview with Acme", ME), ["acme"])
        self.assertEqual(moments.distinctive_words("Flight to New York (TP 202)", ME), ["york"])

    def test_surname_inside_his_address_is_his(self):
        # The profile said only "Sam"; "Mercer" matched every commitment with his name in it.
        self.assertEqual(moments.distinctive_words("Sam Mercer and Kim Park", ME), ["kim", "park"])


class TestTiming(unittest.TestCase):
    def test_meeting_heads_up_window(self):
        m = _meeting(datetime(2026, 11, 5, 21, 0, tzinfo=LOCAL))
        self.assertEqual(_due(m, datetime(2026, 11, 5, 19, 55, tzinfo=LOCAL)), [])
        self.assertEqual(_due(m, datetime(2026, 11, 5, 20, 0, tzinfo=LOCAL)), ["before"])
        self.assertEqual(_due(m, datetime(2026, 11, 5, 20, 49, tzinfo=LOCAL)), ["before"])
        self.assertEqual(_due(m, datetime(2026, 11, 5, 20, 51, tzinfo=LOCAL)), [])   # too late

    def test_invite_in_another_timezone_is_timed_in_his(self):
        # 11:30 AM New York == 8:30 PM in Dubai: the heads-up is due at 7:30 PM his time.
        m = _meeting(datetime(2026, 11, 3, 11, 30, tzinfo=ZoneInfo("America/New_York")),
                     event_tz="America/New_York")
        self.assertEqual(_due(m, datetime(2026, 11, 3, 19, 30, tzinfo=LOCAL)), ["before"])
        text = moments.when_text(m, datetime(2026, 11, 3, 19, 30, tzinfo=LOCAL))
        self.assertIn("8:30 PM your time (11:30 AM in New York)", text)
        self.assertIn("starts in 1h 00m", text)

    def test_morning_after_lands_after_quiet_hours(self):
        m = _meeting(datetime(2026, 11, 5, 22, 0, tzinfo=LOCAL))      # ends 22:30
        self.assertEqual(_due(m, datetime(2026, 11, 6, 0, 30, tzinfo=LOCAL)), [])
        self.assertEqual(_due(m, datetime(2026, 11, 6, 8, 30, tzinfo=LOCAL)), ["after"])
        self.assertEqual(_due(m, datetime(2026, 11, 6, 17, 0, tzinfo=LOCAL)), [])  # stale

    def test_early_flight_day_before_moves_out_of_quiet_hours(self):
        f = Moment(key="f", kind="flight", title="Flight to Lisbon (TP 202)",
                   start=datetime(2026, 11, 14, 5, 45, tzinfo=LOCAL),
                   end=datetime(2026, 11, 14, 8, 0, tzinfo=LOCAL))
        # 24h before is 05:45 — quiet hours — so it speaks at 08:30 that morning instead.
        self.assertEqual(_due(f, datetime(2026, 11, 13, 6, 0, tzinfo=LOCAL)), [])
        self.assertEqual(_due(f, datetime(2026, 11, 13, 8, 30, tzinfo=LOCAL)), ["day_before"])

    def test_late_meeting_heads_up_moves_to_the_evening_before(self):
        m = _meeting(datetime(2026, 11, 6, 0, 30, tzinfo=LOCAL))      # 00:30 start
        self.assertEqual(_due(m, datetime(2026, 11, 5, 22, 30, tzinfo=LOCAL)), ["before"])

    def test_every_kind_has_touchpoints(self):
        self.assertEqual(set(TOUCHPOINTS), {"meeting", "flight", "booking"})


class TestLegwork(unittest.TestCase):
    def test_search_queries(self):
        flight = Moment(key="f", kind="flight", title="Flight to Lisbon (TP 202)",
                        start=None, end=None)
        self.assertEqual(moments.search_query(flight), '"TP 202" OR "TP202"')
        booking = Moment(key="b", kind="booking", title="Reservation at Lily Cafe - Midtown",
                         start=None, end=None)
        self.assertEqual(moments.search_query(booking), '"Lily Cafe"')
        with_people = _meeting(None, people=[{"name": "Kim", "email": "kim@acme.com"}])
        self.assertEqual(moments.search_query(with_people), "from:kim@acme.com OR to:kim@acme.com")
        self.assertEqual(moments.search_query(_meeting(None), ME), "acme")

    def test_gather_skips_calendar_mail_and_matches_commitments_by_word(self):
        m = _meeting(None, title="Sam Mercer and Kim Park", people=[{"name": "Kim Park",
                                                                  "email": "kim@acme.com"}])
        commitments = [
            {"id": 1, "description": "Send Kim the deck", "due_date": "2026-11-05"},
            {"id": 2, "description": "Reply to Sam Mercer's landlord", "due_date": None},
            {"id": 3, "description": "Book parking", "due_date": None}]        # "park" ≠ "parking"
        with patch("skills.email_manager.read_email_thread") as read, \
             patch("skills.commitment_manager.get_open_commitments", return_value=commitments), \
             patch("people._load", return_value=[]):
            read.invoke.return_value = "Matching threads ... Kim: see you Thursday"
            found = moments.gather(m, ME)
        query = read.invoke.call_args[0][0]["search_query"]
        self.assertIn("-filename:ics", query)
        self.assertEqual(found["commitments"], ["#1 Send Kim the deck (due 2026-11-05)"])
        self.assertIn("see you Thursday", found["email"])

    def test_no_substance_means_no_model_call(self):
        solo = _meeting(None, title="Dentist")
        empty = {"email": "", "commitments": [], "people": []}
        self.assertFalse(moments.has_substance(solo, empty))
        self.assertTrue(moments.has_substance(solo, dict(empty, email="thread")))
        self.assertTrue(moments.has_substance(
            _meeting(None, people=[{"name": "Kim", "email": "k@acme.com"}]), empty))


class TestJoinLinkGuard(unittest.TestCase):
    """Live dry run: the model gave the CANCELLED meeting's Zoom link as the latest one."""

    def test_links_must_come_from_the_event_or_the_mail_read(self):
        m = _meeting(None, location="https://acme.zoom.us/j/111?pwd=x")
        self.assertEqual(moments.unknown_call_links("join acme.zoom.us/j/111", m, {"email": ""}), [])
        self.assertEqual(moments.unknown_call_links("use https://acme.zoom.us/j/222?pwd=y", m,
                                                    {"email": ""}), ["222"])
        self.assertEqual(moments.unknown_call_links(
            "https://meet.google.com/abc-defg-hij", m,
            {"email": "Kim: meet.google.com/abc-defg-hij"}), [])
        self.assertEqual(moments.unknown_call_links("see you at 10", m, {"email": ""}), [])


if __name__ == "__main__":
    unittest.main()
