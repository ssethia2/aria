"""Tests for fetch-based calendar edit/delete (no stored IDs).

Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from skills import google_calendar as gc


def _service_with(events_by_cal):
    """events_by_cal: {calendar_id: [event dicts]}."""
    service = MagicMock()
    def list_call(calendarId, **kw):
        m = MagicMock()
        m.execute.return_value = {'items': events_by_cal.get(calendarId, [])}
        return m
    service.events.return_value.list.side_effect = list_call
    return service


def _ev(eid, summary, start_dt):
    return {'id': eid, 'summary': summary, 'start': {'dateTime': start_dt}}


class TempConfig(unittest.TestCase):
    def setUp(self):
        fd, self.cfg = tempfile.mkstemp(suffix='.json'); os.close(fd); os.unlink(self.cfg)
        self._p = patch.object(gc, 'CONFIG_PATH', self.cfg); self._p.start()
        gc._save_config({'shared_calendar_id': 'gf123'})

    def tearDown(self):
        self._p.stop()
        if os.path.exists(self.cfg):
            os.unlink(self.cfg)


class TestDelete(TempConfig):
    def test_deletes_both_copies_of_one_event(self):
        events = {
            'primary': [_ev('p1', 'Dentist', '2026-06-20T15:00:00-04:00')],
            'gf123':   [_ev('s1', 'Dentist', '2026-06-20T15:00:00-04:00')],
        }
        service = _service_with(events)
        with patch.object(gc, 'get_calendar_service', return_value=service):
            msg = gc.delete_calendar_event.invoke({'query': 'dentist'})
        delete = service.events.return_value.delete
        self.assertEqual(delete.call_count, 2)                 # both calendars
        deleted_ids = {c.kwargs['eventId'] for c in delete.call_args_list}
        self.assertEqual(deleted_ids, {'p1', 's1'})
        self.assertIn('2 calendar', msg)

    def test_ambiguous_lists_options(self):
        events = {'primary': [
            _ev('p1', 'Lunch', '2026-06-20T12:00:00-04:00'),
            _ev('p2', 'Dinner', '2026-06-20T19:00:00-04:00')]}
        service = _service_with(events)
        with patch.object(gc, 'get_calendar_service', return_value=service):
            msg = gc.delete_calendar_event.invoke({'query': 'with Priya'})
        self.assertIn('which one', msg.lower())
        service.events.return_value.delete.assert_not_called()

    def test_no_match(self):
        service = _service_with({})
        with patch.object(gc, 'get_calendar_service', return_value=service):
            msg = gc.delete_calendar_event.invoke({'query': 'ghost'})
        self.assertIn('No event', msg)


class TestUpdate(TempConfig):
    def test_rename_patches_both_copies(self):
        events = {
            'primary': [_ev('p1', 'Dinner', '2026-06-20T19:00:00-04:00')],
            'gf123':   [_ev('s1', 'Dinner', '2026-06-20T19:00:00-04:00')],
        }
        service = _service_with(events)
        with patch.object(gc, 'get_calendar_service', return_value=service):
            msg = gc.update_calendar_event.invoke({'query': 'dinner', 'new_title': 'Anniversary dinner'})
        patch_call = service.events.return_value.patch
        self.assertEqual(patch_call.call_count, 2)
        self.assertEqual(patch_call.call_args.kwargs['body']['summary'], 'Anniversary dinner')

    def test_reschedule_sets_new_start(self):
        events = {'primary': [_ev('p1', 'Dentist', '2026-06-20T15:00:00-04:00')]}
        service = _service_with(events)
        with patch.object(gc, 'get_calendar_service', return_value=service):
            gc.update_calendar_event.invoke({'query': 'dentist', 'new_date_iso': '2026-06-21',
                                             'new_start_time': '16:00'})
        body = service.events.return_value.patch.call_args.kwargs['body']
        self.assertIn('2026-06-21T16:00', body['start']['dateTime'])

    def test_nothing_to_change(self):
        events = {'primary': [_ev('p1', 'Dentist', '2026-06-20T15:00:00-04:00')]}
        service = _service_with(events)
        with patch.object(gc, 'get_calendar_service', return_value=service):
            msg = gc.update_calendar_event.invoke({'query': 'dentist'})
        self.assertIn('Nothing to change', msg)


if __name__ == '__main__':
    unittest.main()
