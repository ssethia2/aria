"""Tests for the proactivity engine — quiet hours, dedup, queue/flush, isolation.

All Gmail/LLM/Telegram access is mocked; these run offline.
Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import engine
from engine import (Monitor, Notification, ProactiveEngine, CommitmentMonitor,
                    EmailDigestMonitor, ChaseMonitor, InsightMonitor, NetflixMonitor)


class StubMonitor(Monitor):
    def __init__(self, notifications, name="stub", interval_seconds=60):
        super().__init__(name=name, interval_seconds=interval_seconds)
        self.notifications = notifications
        self.calls = 0

    def check(self, state):
        self.calls += 1
        return self.notifications


class TempStateMixin:
    """Point engine state at a throwaway file for each test."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.unlink(self._tmp.name)  # start with no state file
        self._patcher = patch.object(engine, 'STATE_PATH', self._tmp.name)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self._tmp.name):
            os.unlink(self._tmp.name)


class TestQuietHours(unittest.TestCase):
    def setUp(self):
        self.eng = ProactiveEngine(monitors=[], notify_fn=lambda t: True,
                                   quiet_hours=(23, 8))

    def at(self, hour):
        return datetime(2026, 6, 10, hour, 30)

    def test_wrapping_window(self):
        self.assertTrue(self.eng.in_quiet_hours(self.at(23)))
        self.assertTrue(self.eng.in_quiet_hours(self.at(2)))
        self.assertTrue(self.eng.in_quiet_hours(self.at(7)))
        self.assertFalse(self.eng.in_quiet_hours(self.at(8)))
        self.assertFalse(self.eng.in_quiet_hours(self.at(12)))
        self.assertFalse(self.eng.in_quiet_hours(self.at(22)))


class TestActionMemory(TempStateMixin, unittest.TestCase):
    """Engine notifications must land in the working-memory scratchpad (no split brain)."""

    def test_notification_recorded_to_scratchpad(self):
        import memory
        with tempfile.NamedTemporaryFile(mode='r', suffix='.txt', delete=False) as tmp:
            scratch_path = tmp.name
        try:
            mon = StubMonitor([Notification("📺 Netflix email — I acted on it.\nVisited the link.")])
            eng = ProactiveEngine([mon], notify_fn=lambda t: True)
            with patch.object(memory, 'SCRATCHPAD_PATH', scratch_path):
                eng.tick(now_monotonic=100, now=datetime(2026, 6, 10, 12, 0))
            with open(scratch_path) as f:
                content = f.read()
            self.assertIn("Aria proactive action", content)
            self.assertIn("Netflix email", content)
            self.assertNotIn("\nVisited", content)  # flattened to one scratchpad line
        finally:
            os.unlink(scratch_path)

    def test_quiet_hours_still_record_action(self):
        """The ping queues at night, but the memory record happens immediately."""
        import memory
        with tempfile.NamedTemporaryFile(mode='r', suffix='.txt', delete=False) as tmp:
            scratch_path = tmp.name
        try:
            mon = StubMonitor([Notification("acted at night")])
            eng = ProactiveEngine([mon], notify_fn=lambda t: True, quiet_hours=(23, 8))
            with patch.object(memory, 'SCRATCHPAD_PATH', scratch_path):
                eng.tick(now_monotonic=100, now=datetime(2026, 6, 10, 3, 0))
            with open(scratch_path) as f:
                self.assertIn("acted at night", f.read())
        finally:
            os.unlink(scratch_path)


class TestQueueAndFlush(TempStateMixin, unittest.TestCase):
    def test_quiet_hours_queue_then_morning_flush(self):
        sent = []
        notify = lambda t: sent.append(t) or True
        mon = StubMonitor([Notification("ping")])
        eng = ProactiveEngine([mon], notify_fn=notify, quiet_hours=(23, 8))

        eng.tick(now_monotonic=100, now=datetime(2026, 6, 10, 3, 0))   # night
        self.assertEqual(sent, [])                                      # queued, not sent
        self.assertIn("ping", engine.load_state()["queued_notifications"])

        mon.notifications = []                                          # nothing new
        eng.tick(now_monotonic=10_000, now=datetime(2026, 6, 10, 9, 0))  # morning
        self.assertEqual(len(sent), 1)
        self.assertIn("While you were away", sent[0])
        self.assertIn("ping", sent[0])
        self.assertEqual(engine.load_state()["queued_notifications"], [])

    def test_urgent_bypasses_quiet_hours(self):
        sent = []
        mon = StubMonitor([Notification("fire!", urgent=True)])
        eng = ProactiveEngine([mon], notify_fn=lambda t: sent.append(t) or True,
                              quiet_hours=(23, 8))
        eng.tick(now_monotonic=100, now=datetime(2026, 6, 10, 3, 0))
        self.assertEqual(sent, ["fire!"])

    def test_failed_flush_keeps_queue(self):
        mon = StubMonitor([Notification("ping")])
        eng = ProactiveEngine([mon], notify_fn=lambda t: False,  # delivery down
                              quiet_hours=(23, 8))
        eng.tick(now_monotonic=100, now=datetime(2026, 6, 10, 3, 0))
        mon.notifications = []
        eng.tick(now_monotonic=10_000, now=datetime(2026, 6, 10, 9, 0))
        self.assertIn("ping", engine.load_state()["queued_notifications"])


class TestEngineMechanics(TempStateMixin, unittest.TestCase):
    def test_monitor_interval_respected(self):
        mon = StubMonitor([], interval_seconds=600)
        eng = ProactiveEngine([mon], notify_fn=lambda t: True)
        eng.tick(now_monotonic=0, now=datetime(2026, 6, 10, 12, 0))
        eng.tick(now_monotonic=60, now=datetime(2026, 6, 10, 12, 1))    # too soon
        eng.tick(now_monotonic=700, now=datetime(2026, 6, 10, 12, 12))  # due again
        self.assertEqual(mon.calls, 2)

    def test_one_broken_monitor_does_not_stop_others(self):
        class Broken(Monitor):
            def __init__(self):
                super().__init__(name="broken", interval_seconds=60)

            def check(self, state):
                raise RuntimeError("boom")

        sent = []
        ok = StubMonitor([Notification("still alive")], name="ok")
        eng = ProactiveEngine([Broken(), ok], notify_fn=lambda t: sent.append(t) or True)
        eng.tick(now_monotonic=100, now=datetime(2026, 6, 10, 12, 0))
        self.assertEqual(sent, ["still alive"])


class TestCommitmentMonitor(TempStateMixin, unittest.TestCase):
    def test_timed_commitment_pings_once(self):
        due = [{"id": 1, "description": "Call Mom", "who": "Mom", "due_time": "15:00",
                "recurring": None, "is_recurring": False}]
        mon = CommitmentMonitor(now_fn=lambda: datetime(2026, 6, 10, 15, 1))
        with patch('skills.commitment_manager.get_pingable_now', return_value=due):
            state = {}
            first = mon.check(state)
            second = mon.check(state)
        self.assertEqual(len(first), 1)
        self.assertIn("Call Mom", first[0].text)
        self.assertIn("15:00", first[0].text)
        self.assertEqual(second, [])  # already pinged today

    def test_nothing_pingable_means_silence(self):
        mon = CommitmentMonitor(now_fn=lambda: datetime(2026, 6, 10, 9, 0))
        with patch('skills.commitment_manager.get_pingable_now', return_value=[]):
            self.assertEqual(mon.check({}), [])

    def test_recurring_fires_and_advances_every_tick(self):
        due = [{"id": 5, "description": "HubSpot PERM check-in", "who": "HubSpot",
                "due_time": None, "recurring": "weekly", "is_recurring": True}]
        mon = CommitmentMonitor(now_fn=lambda: datetime(2026, 6, 12, 10, 0))
        with patch('skills.commitment_manager.get_pingable_now', return_value=due), \
             patch('skills.commitment_manager.advance_recurring') as adv:
            state = {}
            out = mon.check(state)
            adv.assert_called_once_with(5, "2026-06-12")   # rolled forward, not deduped
        self.assertIn("repeats weekly", out[0].text)


class TestEmailDigestMonitor(TempStateMixin, unittest.TestCase):
    def _service(self, msg_ids):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value \
            .execute.return_value = {'messages': [{'id': i} for i in msg_ids]}
        service.users.return_value.messages.return_value.get.return_value \
            .execute.return_value = {
                'payload': {'headers': [
                    {'name': 'Subject', 'value': 'Trip plans?'},
                    {'name': 'From', 'value': 'Rohan <rohan@x.com>'}]},
                'snippet': 'hey, any update on the trip?'}
        return service

    def _llm(self, flagged_json):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=flagged_json)
        return llm

    def test_flagged_email_accumulates_and_tracks_reply_without_pinging(self):
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 6, 10, 12, 0))
        state = {}
        with patch('skills.email_manager.get_gmail_service',
                   return_value=self._service(['m1'])), \
             patch.object(engine, '_get_llm', return_value=self._llm(
                 '[{"id": "m1", "reason": "Rohan awaits a reply", "needs_reply": true}]')), \
             patch('skills.commitment_manager.add', return_value=7) as add:
            out = mon.check(state)
        self.assertEqual(out, [])                          # NO instant ping
        self.assertEqual(len(state["pending_digest"]), 1)  # held for the digest
        add.assert_called_once()
        self.assertEqual(add.call_args.kwargs['kind'], 'reply_owed')
        self.assertIn('Rohan', add.call_args.kwargs['who'])

    def test_reply_owed_reconcile_open_thread(self):
        """A thread re-owes a reply only after the prior one was answered."""
        import skills.commitment_manager as cm

        def service_one_email(subject, sender):
            s = MagicMock()
            s.users.return_value.messages.return_value.list.return_value \
                .execute.return_value = {'messages': [{'id': 'e1'}]}
            s.users.return_value.messages.return_value.get.return_value \
                .execute.return_value = {'payload': {'headers': [
                    {'name': 'Subject', 'value': subject},
                    {'name': 'From', 'value': sender}]}, 'snippet': '...'}
            return s

        flagged = '[{"id":"e1","reason":"awaiting reply","needs_reply":true}]'
        llm = MagicMock(); llm.invoke.return_value = MagicMock(content=flagged)
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 6, 13, 12, 0))

        existing = {'id': 7, 'description': 'Reply to Diane: Project X', 'created_at': '2026-06-11 09:00:00'}

        # Case A: prior reply-owed still UNanswered → no new commitment
        with patch('skills.email_manager.get_gmail_service',
                   return_value=service_one_email('Re: Project X', 'Diane <d@x.com>')), \
             patch.object(engine, '_get_llm', return_value=llm), \
             patch('email_backend.using_app_password', return_value=False), \
             patch.object(cm, 'open_reply_owed_for', return_value=existing), \
             patch('skills.email_manager.user_has_replied', return_value=False), \
             patch.object(cm, 'add') as add_a, patch.object(cm, 'complete') as comp_a:
            mon.check({})
        add_a.assert_not_called(); comp_a.assert_not_called()

        # Case B: user already replied to the prior one → close it, open the new one
        with patch('skills.email_manager.get_gmail_service',
                   return_value=service_one_email('Re: Project X', 'Diane <d@x.com>')), \
             patch.object(engine, '_get_llm', return_value=llm), \
             patch('email_backend.using_app_password', return_value=False), \
             patch.object(cm, 'open_reply_owed_for', return_value=existing), \
             patch('skills.email_manager.user_has_replied', return_value=True), \
             patch.object(cm, 'add', return_value=20) as add_b, patch.object(cm, 'complete') as comp_b:
            mon.check({})
        comp_b.assert_called_once_with(7); add_b.assert_called_once()

    def test_bulk_mail_skipped_before_llm(self):
        """A List-Unsubscribe email must be dropped without an LLM call."""
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value \
            .execute.return_value = {'messages': [{'id': 'bulk1'}]}
        service.users.return_value.messages.return_value.get.return_value \
            .execute.return_value = {
                'payload': {'headers': [
                    {'name': 'Subject', 'value': 'Weekly Newsletter'},
                    {'name': 'From', 'value': 'Brew <crew@morningbrew.com>'},
                    {'name': 'List-Unsubscribe', 'value': '<https://unsub.example>'}]},
                'snippet': 'todays news'}
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 6, 12, 12, 0))
        llm = MagicMock()
        with patch('skills.email_manager.get_gmail_service', return_value=service), \
             patch.object(engine, '_get_llm', return_value=llm), \
             patch('email_backend.using_app_password', return_value=False):
            mon.check({})
        llm.invoke.assert_not_called()             # bulk mail never reached the LLM

    def test_evening_flush_sends_one_digest_then_stays_quiet(self):
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 6, 10, 18, 30))
        state = {"pending_digest": [
            {"sender": "Rohan <rohan@x.com>", "subject": "Trip plans?",
             "reason": "awaiting reply", "tracked": "#7"}],
            "seen_ids": [], "last_check_ts": 0}
        with patch('skills.email_manager.get_gmail_service', return_value=self._service([])):
            first = mon.check(state)
            second = mon.check(state)
        self.assertEqual(len(first), 1)
        self.assertIn("Evening inbox digest", first[0].text)
        self.assertIn("Trip plans?", first[0].text)
        self.assertIn("tracking", first[0].text)
        self.assertEqual(second, [])                       # once per day
        self.assertEqual(state["pending_digest"], [])

    def test_empty_pending_means_no_digest(self):
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 6, 10, 18, 30))
        state = {"seen_ids": [], "last_check_ts": 0}
        with patch('skills.email_manager.get_gmail_service', return_value=self._service([])):
            self.assertEqual(mon.check(state), [])


class TestChaseMonitor(TempStateMixin, unittest.TestCase):
    OPEN = [{"id": 3, "description": "Reply to Rohan", "kind": "reply_owed", "who": "Rohan",
             "due_date": "2026-06-08", "due_time": None, "recurring": None,
             "status": "open", "source": "email", "created_at": "2026-06-05 10:00:00"}]

    def _llm(self, verdict_json):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=verdict_json)
        return llm

    def test_nudge_sent_once_per_day_and_ids_recorded(self):
        mon = ChaseMonitor(now_fn=lambda: datetime(2026, 6, 10, 14, 0))
        llm = self._llm('{"send": true, "message": "Rohan is still waiting on you!", "commitment_ids": [3]}')
        state = {}
        with patch('skills.commitment_manager.get_open_commitments', return_value=list(self.OPEN)), \
             patch.object(engine, '_get_llm', return_value=llm):
            first = mon.check(state)
            second = mon.check(state)
        self.assertEqual(len(first), 1)
        self.assertIn("Rohan", first[0].text)
        self.assertEqual(state["nudged"]["3"], "2026-06-10")
        self.assertEqual(second, [])              # one nudge per day, max
        llm.invoke.assert_called_once()           # and no second LLM spend

    def test_silence_verdict_sends_nothing_but_allows_later_check(self):
        mon = ChaseMonitor(now_fn=lambda: datetime(2026, 6, 10, 14, 0))
        llm = self._llm('{"send": false, "message": "", "commitment_ids": []}')
        state = {}
        with patch('skills.commitment_manager.get_open_commitments', return_value=list(self.OPEN)), \
             patch.object(engine, '_get_llm', return_value=llm):
            self.assertEqual(mon.check(state), [])
        self.assertNotIn("nudge_sent_date", state)  # may re-evaluate later today

    def test_no_evaluation_at_night_or_with_nothing_open(self):
        llm = self._llm('{"send": true, "message": "x", "commitment_ids": []}')
        night = ChaseMonitor(now_fn=lambda: datetime(2026, 6, 10, 22, 0))
        with patch('skills.commitment_manager.get_open_commitments', return_value=list(self.OPEN)), \
             patch.object(engine, '_get_llm', return_value=llm):
            self.assertEqual(night.check({}), [])
        day = ChaseMonitor(now_fn=lambda: datetime(2026, 6, 10, 14, 0))
        with patch('skills.commitment_manager.get_open_commitments', return_value=[]), \
             patch.object(engine, '_get_llm', return_value=llm):
            self.assertEqual(day.check({}), [])
        llm.invoke.assert_not_called()


class TestHealthMonitor(TempStateMixin, unittest.TestCase):
    def test_alerts_on_fail_then_dedups(self):
        results = [('gmail', 'FAIL', 'token expired'), ('disk', 'OK', 'fine')]
        mon = engine.HealthMonitor()
        with patch('healthcheck.run_all', return_value=results):
            first = mon.check({})
            state = {}
            a = mon.check(state)
            b = mon.check(state)
        self.assertEqual(len(first), 1)
        self.assertIn("attention", first[0].text.lower())
        self.assertEqual(len(a), 1)
        self.assertEqual(b, [])          # same failure same day → silent

    def test_silent_when_all_ok(self):
        with patch('healthcheck.run_all', return_value=[('x', 'OK', 'fine')]):
            self.assertEqual(engine.HealthMonitor().check({}), [])


class TestInsightMonitor(TempStateMixin, unittest.TestCase):
    def _mon(self, hour=11):
        m = InsightMonitor(now_fn=lambda: datetime(2026, 6, 12, hour, 0))
        m._gather = lambda: "OPEN COMMITMENTS:\n#3 Reply to Priya — due 2026-06-09 OVERDUE"
        return m

    def _llm(self, verdict_json):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=verdict_json)
        return llm

    def test_silent_at_night(self):
        m = InsightMonitor(now_fn=lambda: datetime(2026, 6, 12, 23, 0))
        with patch('llm_router.get_llm') as g:
            self.assertEqual(m.check({}), [])
        g.assert_not_called()

    def test_one_insight_per_halfday(self):
        m = self._mon(hour=11)
        with patch('llm_router.get_llm',
                   return_value=self._llm('{"send": true, "insight": "Free morning — clear the Priya reply."}')):
            state = {}
            first = m.check(state)
            second = m.check(state)
        self.assertEqual(len(first), 1)
        self.assertIn("Priya", first[0].text)
        self.assertEqual(second, [])               # AM slot already used

    def test_silence_verdict_sends_nothing(self):
        m = self._mon()
        with patch('llm_router.get_llm',
                   return_value=self._llm('{"send": false, "insight": ""}')):
            self.assertEqual(m.check({}), [])

    def test_empty_context_skips_without_llm(self):
        m = InsightMonitor(now_fn=lambda: datetime(2026, 6, 12, 11, 0))
        m._gather = lambda: ""
        with patch('llm_router.get_llm') as g:
            self.assertEqual(m.check({}), [])
        g.assert_not_called()

    def test_recent_insights_tracked_for_dedup(self):
        m = self._mon()
        with patch('llm_router.get_llm',
                   return_value=self._llm('{"send": true, "insight": "Rain at 3 — move your run earlier."}')):
            state = {}
            m.check(state)
        self.assertIn("Rain at 3 — move your run earlier.", state["recent"])


class TestNetflixMonitor(TempStateMixin, unittest.TestCase):
    def _service_returning(self, msg_id):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value \
            .execute.return_value = {'messages': [{'id': msg_id}] if msg_id else []}
        return service

    def test_new_email_triggers_automation_once(self):
        mon = NetflixMonitor()
        tool = MagicMock()
        tool.invoke.return_value = "Successfully updated!"
        state = {}
        with patch('skills.netflix_manager.get_netflix_gmail_service',
                   return_value=self._service_returning('abc')), \
             patch('skills.netflix_manager.update_netflix_household', tool):
            first = mon.check(state)
            second = mon.check(state)   # same email id — must not re-fire
        self.assertEqual(len(first), 1)
        self.assertIn("Successfully updated!", first[0].text)
        tool.invoke.assert_called_once()
        self.assertEqual(second, [])

    def test_missing_secondary_token_is_silent(self):
        mon = NetflixMonitor()
        with patch('skills.netflix_manager.get_netflix_gmail_service', return_value=None):
            self.assertEqual(mon.check({}), [])


class TestReflectionMonitor(unittest.TestCase):
    def _llm(self, payload):
        resp = MagicMock(); resp.content = payload
        llm = MagicMock(); llm.invoke.return_value = resp
        return patch('llm_router.get_llm', return_value=llm)

    def _evening(self):
        return datetime(2026, 6, 16, 21, 0)

    def test_proposes_a_standing_rule_in_the_evening(self):
        from engine import ReflectionMonitor
        mon = ReflectionMonitor(now_fn=self._evening)
        with patch.object(ReflectionMonitor, '_todays_memory',
                          return_value="User: always email the news, don't chat it."), \
             patch('instructions.render_for_prompt', return_value=""), \
             self._llm('{"suggestions": ["Always send the news as an email, never a chat."]}'):
            notes = mon.check({})
        self.assertEqual(len(notes), 1)
        self.assertIn("standing rule", notes[0].text.lower())
        self.assertIn("news", notes[0].text.lower())

    def test_silent_before_evening(self):
        from engine import ReflectionMonitor
        mon = ReflectionMonitor(now_fn=lambda: datetime(2026, 6, 16, 12, 0))
        self.assertEqual(mon.check({}), [])

    def test_at_most_once_per_day(self):
        from engine import ReflectionMonitor
        mon = ReflectionMonitor(now_fn=self._evening)
        self.assertEqual(mon.check({"last_date": "2026-06-16"}), [])

    def test_no_suggestions_is_silent_and_burns_the_day(self):
        from engine import ReflectionMonitor
        mon = ReflectionMonitor(now_fn=self._evening)
        slice_ = {}
        with patch.object(ReflectionMonitor, '_todays_memory', return_value="nothing notable"), \
             patch('instructions.render_for_prompt', return_value=""), \
             self._llm('{"suggestions": []}'):
            self.assertEqual(mon.check(slice_), [])
        self.assertEqual(slice_.get("last_date"), "2026-06-16")  # won't re-run today

    def test_does_not_repropose_a_recent_suggestion(self):
        from engine import ReflectionMonitor
        mon = ReflectionMonitor(now_fn=self._evening)
        rule = "Always send the news as an email, never a chat."
        with patch.object(ReflectionMonitor, '_todays_memory', return_value="x"), \
             patch('instructions.render_for_prompt', return_value=""), \
             self._llm('{"suggestions": ["%s"]}' % rule):
            notes = mon.check({"recent": [rule]})
        self.assertEqual(notes, [])


class TestWeeklyReflectionMonitor(unittest.TestCase):
    def _llm(self, payload):
        resp = MagicMock(); resp.content = payload
        llm = MagicMock(); llm.invoke.return_value = resp
        return patch('llm_router.get_llm', return_value=llm)

    def _sunday_eve(self):
        return datetime(2026, 6, 21, 19, 0)   # a Sunday, 7pm

    def test_surfaces_note_and_rule_on_sunday_evening(self):
        from engine import WeeklyReflectionMonitor
        mon = WeeklyReflectionMonitor(now_fn=self._sunday_eve)
        with patch.object(WeeklyReflectionMonitor, '_signal',
                          return_value="COMMITMENT PATTERNS:\n- replies to Diane keep slipping"), \
             patch('instructions.render_for_prompt', return_value=""), \
             self._llm('{"note": "your replies keep slipping midweek", "rule": "Chase reply-owed items after 2 days."}'):
            notes = mon.check({})
        self.assertEqual(len(notes), 2)                    # the note + the rule proposal
        self.assertIn("week", notes[0].text.lower())
        self.assertIn("standing rule", notes[1].text.lower())

    def test_silent_when_not_sunday(self):
        from engine import WeeklyReflectionMonitor
        mon = WeeklyReflectionMonitor(now_fn=lambda: datetime(2026, 6, 17, 19, 0))  # Wednesday
        self.assertEqual(mon.check({}), [])

    def test_once_per_week(self):
        from engine import WeeklyReflectionMonitor
        mon = WeeklyReflectionMonitor(now_fn=self._sunday_eve)
        self.assertEqual(mon.check({"last_week": datetime(2026, 6, 21).strftime('%Y-%U')}), [])

    def test_empty_signal_is_silent(self):
        from engine import WeeklyReflectionMonitor
        mon = WeeklyReflectionMonitor(now_fn=self._sunday_eve)
        with patch.object(WeeklyReflectionMonitor, '_signal', return_value=""):
            self.assertEqual(mon.check({}), [])


if __name__ == '__main__':
    unittest.main()
