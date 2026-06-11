"""Tests for the Google Calendar connector — dual-write rule, color, dedupe.

Google API is mocked; these run offline.
Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from skills import google_calendar as gc


class TempConfigMixin:
    def setUp(self):
        fd, self.cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.cfg)
        self._p = patch.object(gc, 'CONFIG_PATH', self.cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        if os.path.exists(self.cfg):
            os.unlink(self.cfg)


def _service():
    service = MagicMock()
    service.events.return_value.insert.return_value.execute.return_value = {'id': 'ev1'}
    return service


class TestStandingRule(TempConfigMixin, unittest.TestCase):
    def test_event_lands_on_both_calendars_yellow_on_shared_only(self):
        gc._save_config({'shared_calendar_id': 'gf123', 'shared_calendar_label': 'us'})
        service = _service()
        with patch.object(gc, 'get_calendar_service', return_value=service):
            msg = gc.create_calendar_event.invoke({
                'title': 'Dinner', 'date_iso': '2026-06-14', 'start_time': '19:00'})

        calls = service.events.return_value.insert.call_args_list
        self.assertEqual(len(calls), 2)
        by_cal = {c.kwargs['calendarId']: c.kwargs['body'] for c in calls}
        self.assertIn('primary', by_cal)
        self.assertIn('gf123', by_cal)
        self.assertNotIn('colorId', by_cal['primary'])          # personal: default color
        self.assertEqual(by_cal['gf123']['colorId'], gc.YELLOW)  # shared: yellow
        self.assertIn('personal', msg)
        self.assertIn('us (yellow)', msg)

    def test_unconfigured_shared_creates_personal_and_flags_setup(self):
        service = _service()
        with patch.object(gc, 'get_calendar_service', return_value=service):
            msg = gc.create_calendar_event.invoke({
                'title': 'Dentist', 'date_iso': '2026-06-15'})
        self.assertEqual(service.events.return_value.insert.call_count, 1)
        self.assertIn("isn't configured", msg)

    def test_timed_event_has_times_allday_has_dates(self):
        timed = gc._event_body('X', '2026-06-14', '19:00', '21:00')
        self.assertIn('dateTime', timed['start'])
        self.assertIn('2026-06-14T19:00', timed['start']['dateTime'])
        self.assertIn('2026-06-14T21:00', timed['end']['dateTime'])
        allday = gc._event_body('Y', '2026-06-14')
        self.assertEqual(allday['start'], {'date': '2026-06-14'})
        self.assertEqual(allday['end'], {'date': '2026-06-15'})

    def test_default_duration_is_one_hour(self):
        body = gc._event_body('X', '2026-06-14', '19:00')
        self.assertIn('2026-06-14T20:00', body['end']['dateTime'])

    def test_bad_date_rejected_before_any_api_call(self):
        service = _service()
        with patch.object(gc, 'get_calendar_service', return_value=service):
            msg = gc.create_calendar_event.invoke({
                'title': 'X', 'date_iso': 'Saturday'})
        self.assertIn('Error', msg)
        service.events.assert_not_called()


class TestFetchDedupe(TempConfigMixin, unittest.TestCase):
    def test_mirrored_events_collapse_to_one_line(self):
        gc._save_config({'shared_calendar_id': 'gf123'})
        service = MagicMock()
        event = {'summary': 'Dinner', 'start': {'dateTime': '2026-06-14T19:00:00-07:00'}}
        service.events.return_value.list.return_value.execute.return_value = {
            'items': [event]}
        with patch.object(gc, 'get_calendar_service', return_value=service):
            lines = gc.fetch_events(days=1)
        self.assertEqual(len(lines), 1)      # appears on both calendars, listed once
        self.assertIn('Dinner', lines[0])

    def test_no_service_returns_none(self):
        with patch.object(gc, 'get_calendar_service', return_value=None):
            self.assertIsNone(gc.fetch_events())


class TestConfig(TempConfigMixin, unittest.TestCase):
    def test_configure_tool_roundtrip(self):
        msg = gc.configure_shared_calendar.invoke({
            'calendar_id': 'abc@group.calendar.google.com', 'label': 'Satvik+GF'})
        self.assertIn('Saved', msg)
        self.assertEqual(gc._load_config()['shared_calendar_id'],
                         'abc@group.calendar.google.com')


if __name__ == '__main__':
    unittest.main()
