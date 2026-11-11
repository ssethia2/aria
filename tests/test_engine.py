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
import memory

# Every delivered notification is mirrored into the working-memory scratchpad. Point it at a
# throwaway file for this whole module so test pings never land in the real
# daily_scratchpad.txt (they did: "ping", "fire!", "still alive" on every run).
_REAL_SCRATCHPAD = memory.SCRATCHPAD_PATH
_scratch_patch = None


def setUpModule():
    global _scratch_patch
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    _scratch_patch = patch.object(memory, "SCRATCHPAD_PATH", path)
    _scratch_patch.start()


def tearDownModule():
    os.unlink(memory.SCRATCHPAD_PATH)
    _scratch_patch.stop()


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
        import situation
        self._sit = patch.object(situation, 'around',
                                 return_value={"calendar": [], "mail": {}})
        self._sit.start()
        self.addCleanup(self._sit.stop)
        self._motion = patch.object(situation, 'in_motion', return_value={
            "calendar": "(nothing)", "commitments": "(none)"})
        self._motion.start()
        self._known = patch.object(situation, 'known_about', return_value={})
        self._known.start()
        self.addCleanup(self._known.stop)
        self.addCleanup(self._motion.stop)

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

    def test_tests_never_write_the_real_scratchpad(self):
        self.assertNotEqual(memory.SCRATCHPAD_PATH, _REAL_SCRATCHPAD)
        eng = ProactiveEngine([StubMonitor([Notification("ping")])], notify_fn=lambda t: True)
        eng.tick(now_monotonic=100, now=datetime(2026, 6, 10, 12, 0))
        with open(memory.SCRATCHPAD_PATH) as f:
            self.assertIn("ping", f.read())         # went to the module's throwaway file

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


class TestChaseSnooze(TempStateMixin, unittest.TestCase):
    def test_snoozed_items_never_reach_the_chase_llm(self):
        # "Stop bugging me until next week" must actually stop the bugging.
        snoozed = [{"id": 1, "description": "renew passport", "who": None, "kind": "promise",
                    "due_date": "2026-07-01", "due_time": None, "recurring": None,
                    "status": "open", "source": "chat", "created_at": "2026-06-01 10:00:00",
                    "snoozed_until": "2026-08-01"}]
        mon = ChaseMonitor(now_fn=lambda: datetime(2026, 7, 13, 12, 0))
        llm = MagicMock()
        with patch("skills.commitment_manager.get_open_commitments", return_value=snoozed), \
             patch("skills.commitment_manager.slip_counts", return_value={}), \
             patch.object(engine, "_get_llm", return_value=llm):
            out = mon.check({})
        self.assertEqual(out, [])
        llm.invoke.assert_not_called()      # nothing left to judge → no LLM call at all

    def test_recently_nudged_items_wait_three_days_unless_due_today(self):
        """It nudged him about the same assessment nine days running."""
        def item(cid, due):
            return {"id": cid, "description": f"item {cid}", "who": None, "kind": "deadline",
                    "due_date": due, "due_time": None, "recurring": None, "status": "open",
                    "source": "chat", "created_at": "2026-10-01 10:00:00", "snoozed_until": None}
        items = [item(1, "2026-10-31"), item(2, "2026-10-30"), item(3, "2026-11-05")]
        state = {"nudged": {"1": "2026-10-29", "2": "2026-10-29", "3": "2026-10-27"}}
        mon = ChaseMonitor(now_fn=lambda: datetime(2026, 10, 30, 12, 0))
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content='{"send": false}')
        with patch("skills.commitment_manager.get_open_commitments", return_value=items), \
             patch("skills.commitment_manager.slip_counts", return_value={}), \
             patch.object(engine, "_get_llm", return_value=llm):
            mon.check(state)
        offered = llm.invoke.call_args[0][0]
        self.assertNotIn('"item 1"', offered)    # nudged yesterday, due tomorrow → waits
        self.assertIn('"item 2"', offered)       # nudged yesterday, but due TODAY
        self.assertIn('"item 3"', offered)       # last nudged 3 days ago → fair game

    def test_nothing_offered_means_no_llm_call(self):
        items = [{"id": 1, "description": "item 1", "who": None, "kind": "deadline",
                  "due_date": "2026-11-05", "due_time": None, "recurring": None,
                  "status": "open", "source": "chat", "created_at": "2026-10-01 10:00:00",
                  "snoozed_until": None}]
        mon = ChaseMonitor(now_fn=lambda: datetime(2026, 10, 30, 12, 0))
        llm = MagicMock()
        with patch("skills.commitment_manager.get_open_commitments", return_value=items), \
             patch("skills.commitment_manager.slip_counts", return_value={}), \
             patch.object(engine, "_get_llm", return_value=llm):
            self.assertEqual(mon.check({"nudged": {"1": "2026-10-29"}}), [])
        llm.invoke.assert_not_called()


class TestNetflixQuiet(unittest.TestCase):
    def _run(self, result):
        svc = MagicMock()
        svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "newmsg"}]}
        with patch("skills.netflix_manager.get_netflix_gmail_service", return_value=svc), \
             patch("skills.netflix_manager.update_netflix_household") as upd:
            upd.invoke.return_value = result
            return NetflixMonitor().check({})

    def test_clean_success_is_silent(self):
        out = self._run("✅ Verified: Netflix's page confirmed the household update.")
        self.assertEqual(out, [])                       # no flood on routine success

    def test_failure_still_pings(self):
        out = self._run("The link led to a Netflix login wall, so I can't complete this one.")
        self.assertEqual(len(out), 1)                   # needs the user -> notify
        self.assertIn("couldn't finish", out[0].text)


class TestRuleRedundancy(unittest.TestCase):
    def test_overlapping_rule_is_redundant(self):
        existing = "Pack blankets and binoculars for trips to Maine."
        self.assertTrue(engine._rule_redundant(
            "Remember to pack blankets and binoculars for Maine trips.", existing))

    def test_novel_rule_is_not_redundant(self):
        existing = "Pack blankets for Maine trips."
        self.assertFalse(engine._rule_redundant(
            "Send Kim a status update every Friday afternoon.", existing))

    def test_paraphrase_of_prior_proposal_is_redundant(self):
        # The actual repeat: the same trip-prep rule proposed 3x in different wording.
        prior = ("When prepping for trips with limited connectivity, proactively flag "
                 "overdue emails and time-sensitive replies 24 hours in advance so they "
                 "can be cleared before going offline.")
        variant = ("When user is traveling with limited connectivity, proactively flag "
                   "overdue emails and time-sensitive replies 24 hours in advance.")
        self.assertTrue(engine._rule_redundant(variant, prior))


class TestNoreplyFilter(unittest.TestCase):
    def test_automated_senders_detected(self):
        from email_filter import is_noreply_sender
        for f in ("PNC Bank <no-reply@pnc.com>", "Expedia <noreply@expedia.com>",
                  "FIFA <do-not-reply@fifa.com>", "Airbnb <automated@airbnb.com>",
                  "GitHub <notifications@github.com>"):
            self.assertTrue(is_noreply_sender(f), f)

    def test_humans_pass(self):
        from email_filter import is_noreply_sender
        for f in ("Kim <kim@company.com>", "Rohan <rohan@x.com>",
                  "Ashley Mareira <ash@gmail.com>"):
            self.assertFalse(is_noreply_sender(f), f)


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
        self.assertTrue(first[0].urgent)   # explicit due_time -> alarm; bypasses quiet hours
        self.assertEqual(second, [])  # already pinged today

    def test_timed_reminder_fires_through_quiet_hours(self):
        # The actual miss: a 3 AM "leave for airport" reminder must NOT be queued until 08:00.
        due = [{"id": 44, "description": "Leave keys at front desk", "who": None,
                "due_time": "03:00", "recurring": None, "is_recurring": False}]
        sent = []
        mon = CommitmentMonitor(now_fn=lambda: datetime(2026, 6, 24, 3, 0))
        eng = ProactiveEngine([mon], notify_fn=lambda t: sent.append(t) or True,
                              quiet_hours=(23, 8))
        with patch('skills.commitment_manager.get_pingable_now', return_value=due):
            eng.tick(now=datetime(2026, 6, 24, 3, 0))
        self.assertTrue(any("Leave keys" in t for t in sent))   # delivered now, not queued

    def test_dateonly_reminder_stays_non_urgent(self):
        due = [{"id": 9, "description": "Renew passport", "who": None,
                "due_time": None, "recurring": None, "is_recurring": False}]
        mon = CommitmentMonitor(now_fn=lambda: datetime(2026, 6, 10, 10, 0))
        with patch('skills.commitment_manager.get_pingable_now', return_value=due):
            out = mon.check({})
        self.assertFalse(out[0].urgent)   # ambient, no user-set time -> respects quiet hours

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
    def setUp(self):
        super().setUp()
        import skills.commitment_manager as cm
        fd, self._db = tempfile.mkstemp(suffix=".db")   # never the real commitments store
        os.close(fd)
        os.unlink(self._db)
        self._db_patch = patch.object(cm, 'DB_PATH', self._db)
        self._db_patch.start()
        import instructions                              # never his real standing rules
        self._rules = os.path.join(tempfile.mkdtemp(), "instructions.json")
        self._rules_patch = patch.object(instructions, 'INSTRUCTIONS_PATH', self._rules)
        self._rules_patch.start()

    def tearDown(self):
        self._rules_patch.stop()
        self._db_patch.stop()
        if os.path.exists(self._db):
            os.unlink(self._db)
        super().tearDown()

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
        # No fabricated deadline: the sender set none, so we track it UNDATED
        # ("I was making up due dates" — never again).
        self.assertIsNone(add.call_args.kwargs.get('due_date'))

    def test_noreply_sender_never_tracked_as_reply_owed(self):
        # PNC/Expedia/FIFA-style automated mail: flag-worthy, but you can't owe a
        # reply to a no-reply address — even when the screening LLM says needs_reply.
        service = self._service(['m2'])
        service.users.return_value.messages.return_value.get.return_value \
            .execute.return_value = {
                'payload': {'headers': [
                    {'name': 'Subject', 'value': 'Your recurring payment changed'},
                    {'name': 'From', 'value': 'PNC Bank <no-reply@pnc.com>'}]},
                'snippet': 'Please review your payment change'}
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 6, 10, 12, 0))
        state = {}
        with patch('skills.email_manager.get_gmail_service', return_value=service), \
             patch.object(engine, '_get_llm', return_value=self._llm(
                 '[{"id": "m2", "reason": "payment change", "needs_reply": true}]')), \
             patch('skills.commitment_manager.add') as add:
            mon.check(state)
        add.assert_not_called()                            # never tracked
        self.assertEqual(len(state["pending_digest"]), 1)  # but still surfaced in digest

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

        existing = {'id': 7, 'description': 'Reply to Kim: Launch plan', 'created_at': '2026-06-11 09:00:00'}

        # Case A: prior reply-owed still UNanswered → no new commitment
        with patch('skills.email_manager.get_gmail_service',
                   return_value=service_one_email('Re: Launch plan', 'Kim <d@x.com>')), \
             patch.object(engine, '_get_llm', return_value=llm), \
             patch('email_backend.using_app_password', return_value=False), \
             patch.object(cm, 'open_reply_owed_for', return_value=existing), \
             patch('skills.email_manager.user_has_replied', return_value=False), \
             patch.object(cm, 'add') as add_a, patch.object(cm, 'complete') as comp_a:
            mon.check({})
        add_a.assert_not_called(); comp_a.assert_not_called()

        # Case B: user already replied to the prior one → close it, open the new one
        with patch('skills.email_manager.get_gmail_service',
                   return_value=service_one_email('Re: Launch plan', 'Kim <d@x.com>')), \
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

    # --- what counts as a reply owed ---

    def _screen_one(self, subject, sender, verdict, rules=None):
        import json as _json
        if rules is not None:
            with open(self._rules, "w") as f:
                _json.dump([{"id": i + 1, "instruction": r} for i, r in enumerate(rules)], f)
        service = self._mail(inbox=[('m1', subject, sender, '...')])
        llm = MagicMock()
        llm.invoke.side_effect = [MagicMock(content=verdict), MagicMock(content='[]')]
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 10, 30, 12, 0))
        state = {}
        with patch('skills.email_manager.get_gmail_service', return_value=service), \
             patch.object(engine, '_get_llm', return_value=llm), \
             patch('email_backend.using_app_password', return_value=False), \
             patch('skills.commitment_manager.add', return_value=9) as add:
            out = mon.check(state)
        return out, state, add, llm.invoke.call_args_list[0][0][0]

    def test_calendar_invites_and_payment_requests_are_never_replies_owed(self):
        """Every one of these in Aria's history was dropped by hand."""
        for subject, sender in [("Invitation: Sam / Jo @ Tue Nov 10, 2026 1pm", "Jo <jo@acme.com>"),
                                ("Sam requests $36.00", "Venmo <venmo@venmo.com>")]:
            out, state, add, _ = self._screen_one(
                subject, sender, '[{"id": "m1", "reason": "r", "needs_reply": true}]')
            add.assert_not_called()
            self.assertEqual(len(state["pending_digest"]), 1)   # still surfaced in the digest

    def test_screener_sees_his_standing_rules(self):
        _, _, _, prompt = self._screen_one(
            "Re: 12 Oak St inquiry", "Agent <a@realty.com>", '[]',
            rules=["Never track apartment emails as reply-owed commitments."])
        self.assertIn("Never track apartment emails as reply-owed commitments.", prompt)

    def test_screener_knows_when_he_is_already_in_the_thread(self):
        import situation
        # A recruiter he'd been writing to for a week was digested as "cold outreach he can
        # ignore per standing rules" — there was no such rule, and he'd replied.
        threads = MagicMock()
        threads.get.return_value.execute.return_value = {'messages': [
            {'id': 'a', 'labelIds': ['INBOX'], 'snippet': 'Can you do Thursday?'},
            {'id': 'b', 'labelIds': ['SENT'], 'snippet': 'Thursday 2pm works'},
            {'id': 'm1', 'labelIds': ['INBOX'], 'snippet': 'Invite sent'}]}
        service = MagicMock()
        service.users.return_value.threads.return_value = threads
        earlier = situation.thread_so_far(service, 't1', 'm1')
        self.assertEqual([m['direction'] for m in earlier], ['received', 'sent'])
        self.assertEqual(earlier[1]['snippet'], 'Thursday 2pm works')
        self.assertEqual(situation.thread_so_far(None, 't1'), [])
        _, _, _, prompt = self._screen_one("RE: Acme recruiting", "Kim <kim@acme.com>", '[]')
        self.assertIn('"he_replied_in_thread"', prompt)
        self.assertIn("Never cite", prompt)

    def test_screener_reads_mail_against_memory_and_what_is_in_motion(self):
        import situation
        with patch.object(situation, 'known_about',
                          return_value={"you_remember": "- interviewing with Acme"}), \
             patch.object(situation, 'in_motion', return_value={
                 "calendar": "Thu Nov 12, 2:00 PM — Acme interview", "commitments": "(none)"}):
            _, _, _, prompt = self._screen_one("Acme interview", "Kim <kim@acme.com>", '[]')
        self.assertIn("interviewing with Acme", prompt)
        self.assertIn("Thu Nov 12, 2:00 PM — Acme interview", prompt)
        self.assertIn("one moving part", prompt)

    def test_rule_asked_alert_goes_out_now_not_in_the_digest(self):
        """'Alert me right away about interview invites' was saved, then never honored."""
        out, state, _, _ = self._screen_one(
            "Acme Interview Request", "Mike <mike@acme.com>",
            '[{"id": "m1", "reason": "interview invite", "needs_reply": false, "alert_now": true}]',
            rules=["Alert immediately when an interview invite arrives."])
        self.assertEqual(len(out), 1)
        self.assertIn("Acme Interview Request", out[0].text)
        self.assertNotIn("pending_digest", state)

    # --- closing commitments that email proves done ---

    def _mail(self, inbox=(), sent=()):
        """A Gmail stub: `inbox` / `sent` are (id, subject, sender_or_to, snippet) tuples."""
        msgs = {m[0]: m for m in (*inbox, *sent)}
        service = MagicMock()
        api = service.users.return_value.messages.return_value
        api.list.side_effect = lambda **kw: MagicMock(execute=MagicMock(return_value={
            'messages': [{'id': m[0]} for m in (sent if 'in:sent' in kw['q'] else inbox)]}))
        api.get.side_effect = lambda **kw: MagicMock(execute=MagicMock(return_value={
            'payload': {'headers': [{'name': 'Subject', 'value': msgs[kw['id']][1]},
                                    {'name': 'From', 'value': msgs[kw['id']][2]},
                                    {'name': 'To', 'value': msgs[kw['id']][2]}]},
            'snippet': msgs[kw['id']][3]}))
        return service

    def _check(self, service, *llm_replies):
        llm = MagicMock()
        llm.invoke.side_effect = [MagicMock(content=r) for r in llm_replies]
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 10, 30, 12, 0))
        with patch('skills.email_manager.get_gmail_service', return_value=service), \
             patch.object(engine, '_get_llm', return_value=llm), \
             patch('email_backend.using_app_password', return_value=False):
            return mon.check({}), llm

    def test_inbound_proof_closes_the_commitment_and_says_so(self):
        """The assessment miss: the test platform's 'thanks for taking the test' arrived, and she kept
        nudging him about the assessment for two more days."""
        import skills.commitment_manager as cm
        cid = cm.add("Acme coding assessment", kind='deadline', due_date='2026-10-31')
        service = self._mail(inbox=[('m1', 'Thanks for taking the Acme Hiring Test (Java)',
                                     'Acme Hiring Team <tests@assess.example.com>',
                                     'Submission Confirmation. Thanks for completing the test.')])
        out, llm = self._check(service, '[]', f'[{{"commitment_id": {cid}, "email_id": "m1", '
                                              f'"evidence": "the test platform confirmed your submission"}}]')
        self.assertEqual(cm.get_open_commitments(), [])
        self.assertEqual(len(out), 1)
        self.assertIn("Marked done: Acme coding assessment", out[0].text)
        self.assertIn("reopen", out[0].text)
        self.assertIn(f"#{cid} Acme coding assessment (due 2026-10-31)", llm.invoke.call_args[0][0])

    def test_his_own_sent_mail_counts_as_proof(self):
        import skills.commitment_manager as cm
        cid = cm.add("Acme coding assessment", kind='deadline', due_date='2026-10-31')
        service = self._mail(sent=[('s1', 'Re: Acme - Next Steps', 'Mike <mike@acme.com>',
                                    'Hi Mike, I just completed the virtual assessment.')])
        out, llm = self._check(service, f'[{{"commitment_id": {cid}, "email_id": "s1", '
                                         f'"evidence": "you told Mike you completed it"}}]')
        self.assertEqual(llm.invoke.call_count, 1)        # no inbound → no digest screening
        self.assertIn('"direction": "sent"', llm.invoke.call_args[0][0])
        self.assertEqual(cm.get_open_commitments(), [])
        self.assertIn("(your email:", out[0].text)

    def test_no_proof_leaves_it_open_and_quiet(self):
        import skills.commitment_manager as cm
        cm.add("Acme coding assessment", kind='deadline', due_date='2026-10-31')
        service = self._mail(inbox=[('m1', 'Your Acme Hiring Test Invitation',
                                     'Acme <tests@assess.example.com>', 'Please complete')])
        out, _ = self._check(service, '[]', '[]')
        self.assertEqual(out, [])
        self.assertEqual(len(cm.get_open_commitments()), 1)

    def test_made_up_ids_close_nothing(self):
        import skills.commitment_manager as cm
        cid = cm.add("Acme coding assessment", kind='deadline')
        service = self._mail(inbox=[('m1', 'Thanks for taking the test', 'HR <hr@acme.com>', 'done')])
        out, _ = self._check(service, '[]', f'[{{"commitment_id": {cid + 99}, "email_id": "m1"}}, '
                                            f'{{"commitment_id": {cid}, "email_id": "nope"}}]')
        self.assertEqual(out, [])
        self.assertEqual(len(cm.get_open_commitments()), 1)

    def test_proof_pinned_on_the_wrong_commitment_is_rejected(self):
        """From a real dry run: the model tied an "I just completed the assessment" email
        to an unrelated item. The email has to name the commitment it closes."""
        import skills.commitment_manager as cm
        wrong = cm.add("Renew prescription", kind='promise')
        right = cm.add("Acme coding assessment", kind='deadline')
        service = self._mail(sent=[('s1', 'Re: Acme - Next Steps', 'Mike <mike@acme.com>',
                                    'Hi Mike, I just completed the virtual assessment.')])
        out, _ = self._check(service, f'[{{"commitment_id": {wrong}, "email_id": "s1", '
                                      f'"evidence": "completed the assessment"}}]')
        self.assertEqual(out, [])
        self.assertEqual({c['id'] for c in cm.get_open_commitments()}, {wrong, right})

    def test_words_shared_by_two_commitments_dont_count_as_naming_either(self):
        """'wedding gift' is in both gift items — only 'vivek' says which one."""
        import skills.commitment_manager as cm
        swastik = cm.add("Swastik & Pulkit wedding gift (urgent)", kind='promise')
        vivek = cm.add("Buy Vivek's wedding gift (urgent)", kind='promise')
        service = self._mail(inbox=[('m1', 'Your order: wedding gift for Vivek',
                                     'Shop <orders@shop.com>', 'Your wedding gift order shipped')])
        out, _ = self._check(service, '[]', f'[{{"commitment_id": {swastik}, "email_id": "m1"}}, '
                                            f'{{"commitment_id": {vivek}, "email_id": "m1"}}]')
        self.assertEqual([c['id'] for c in cm.get_open_commitments()], [swastik])
        self.assertEqual(len(out), 1)
        self.assertIn("Vivek", out[0].text)

    def test_replies_owed_and_recurring_items_are_never_offered(self):
        """reply_owed has its own sent-mail check; birthdays roll forward, never close."""
        import skills.commitment_manager as cm
        cm.add("Reply to Rohan: Trip plans?", kind='reply_owed')
        cm.add("Mom's birthday", kind='people_date', due_date='2027-03-14', recurring='yearly')
        service = self._mail(sent=[('s1', 'Re: Trip plans?', 'Rohan <r@x.com>', 'Sounds good')])
        out, llm = self._check(service)
        llm.invoke.assert_not_called()
        self.assertEqual(len(cm.get_open_commitments()), 2)

    def test_proof_check_failure_never_blocks_the_digest(self):
        mon = EmailDigestMonitor(now_fn=lambda: datetime(2026, 6, 10, 18, 30))
        state = {"pending_digest": [{"sender": "Rohan <r@x.com>", "subject": "Trip plans?",
                                     "reason": "awaiting reply"}]}
        with patch.object(mon, '_screen_new_mail', return_value=[]), \
             patch.object(mon, '_close_proven', side_effect=RuntimeError("Gmail down")):
            out = mon.check(state)
        self.assertEqual(len(out), 1)
        self.assertIn("Evening inbox digest", out[0].text)

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
                          return_value="COMMITMENT PATTERNS:\n- replies to Kim keep slipping"), \
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


class TestReconcile(TempStateMixin, unittest.TestCase):
    """She nudged him to schedule an interview already on his calendar, and to answer a
    thread he had answered: the commitment row was judged alone."""

    def _items(self):
        base = {"who": None, "due_date": None, "due_time": None, "recurring": None,
                "status": "open", "source": "chat", "created_at": "2026-11-03 10:00:00"}
        return [dict(base, id=1, kind="promise", description="Schedule Acme interview"),
                dict(base, id=2, kind="reply_owed", who="Kim",
                     description="Reply to Kim: Northwind onboarding"),
                dict(base, id=3, kind="promise", description="Prepare for Porto trip")]

    def _run(self, verdict, sit):
        import situation
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=verdict)
        done = []
        with patch.object(situation, 'around', return_value=sit), \
             patch('skills.commitment_manager.get_open_commitments', return_value=self._items()), \
             patch('skills.commitment_manager.complete', side_effect=lambda cid: done.append(cid) or True), \
             patch('llm_router.get_llm', return_value=llm):
            notes = engine.reconcile(datetime(2026, 11, 10, 12, 0))
        return done, notes, llm

    def _mail(self, direction, subject, when):
        return {'id': 'x', 'direction': direction, 'who': 'Kim <kim@northwind.example>',
                'subject': subject, 'snippet': 'sounds good', 'hits': 1,
                'ts': datetime.fromisoformat(when).timestamp()}

    def test_calendar_event_closes_a_scheduling_item(self):
        sit = {"calendar": ["Thu Nov 12, 2:00 PM — Acme interview"], "mail": {}}
        done, notes, llm = self._run(
            '[{"commitment_id": 1, "source": "calendar", "quote": "Acme interview", "evidence": "already booked"}]', sit)
        self.assertEqual(done, [1])
        self.assertIn("your calendar", notes[0].text)
        self.assertIn("reopen", notes[0].text)
        self.assertIn("Thu Nov 12, 2:00 PM — Acme interview", llm.invoke.call_args[0][0])

    def test_calendar_event_never_closes_something_to_prepare(self):
        sit = {"calendar": ["Sat Nov 14 (all day) — Porto trip"], "mail": {}}
        done, _, _ = self._run(
            '[{"commitment_id": 3, "source": "calendar", "quote": "Porto trip", "evidence": "on calendar"}]', sit)
        self.assertEqual(done, [])

    def test_his_later_reply_closes_a_reply_owed(self):
        sit = {"calendar": [], "mail": {2: [self._mail('sent', 'Re: Northwind onboarding', '2026-11-05 09:00:00')]}}
        done, notes, _ = self._run(
            '[{"commitment_id": 2, "source": "mail", "quote": "Re: Northwind onboarding", "evidence": "he replied"}]', sit)
        self.assertEqual(done, [2])
        self.assertIn("your email", notes[0].text)

    def test_booked_event_closes_the_reply_owed_that_arranged_it(self):
        # He answered, they confirmed and sent the invite, and the screener then tracked
        # the confirmation as a new reply owed.
        sit = {"calendar": ["Mon Nov 16, 1:00 PM — Northwind onboarding call"], "mail": {}}
        done, notes, _ = self._run(
            '[{"commitment_id": 2, "source": "calendar", "quote": "Northwind onboarding call", "evidence": "booked"}]', sit)
        self.assertEqual(done, [2])

    def test_received_or_older_mail_is_not_his_reply(self):
        for m in (self._mail('received', 'Re: Northwind onboarding', '2026-11-05 09:00:00'),
                  self._mail('sent', 'Re: Northwind onboarding', '2026-11-01 09:00:00')):
            done, _, _ = self._run(
                '[{"commitment_id": 2, "source": "mail", "quote": "Re: Northwind onboarding", "evidence": "replied"}]',
                {"calendar": [], "mail": {2: [m]}})
            self.assertEqual(done, [])

    def test_invented_evidence_closes_nothing(self):
        sit = {"calendar": ["Thu Nov 12, 2:00 PM — Dentist"], "mail": {}}
        done, _, _ = self._run(
            '[{"commitment_id": 1, "source": "calendar", "quote": "Acme interview", "evidence": "booked"}]', sit)
        self.assertEqual(done, [])

    def test_no_evidence_means_no_model_call(self):
        done, notes, llm = self._run('[]', {"calendar": [], "mail": {}})
        self.assertEqual((done, notes), ([], []))
        llm.invoke.assert_not_called()

    def test_chase_judge_sees_mail_and_calendar(self):
        import situation
        mon = ChaseMonitor(now_fn=lambda: datetime(2026, 11, 10, 14, 0))
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content='{"send": false}')
        with patch('skills.commitment_manager.get_open_commitments', return_value=self._items()), \
             patch('skills.commitment_manager.slip_counts', return_value={}), \
             patch.object(engine, 'reconcile', return_value=[]), \
             patch.object(situation, 'describe', return_value="On his calendar: Acme interview"), \
             patch.object(engine, '_get_llm', return_value=llm):
            mon.check({})
        prompt = llm.invoke.call_args[0][0]
        self.assertIn("On his calendar: Acme interview", prompt)
        self.assertIn("one moving part", prompt)



if __name__ == '__main__':
    unittest.main()


class TestMomentMonitor(TempStateMixin, unittest.TestCase):
    """Heads-ups around calendar moments: legwork first, judged once, never a wrong link."""

    def setUp(self):
        super().setUp()
        from zoneinfo import ZoneInfo
        import moments
        self.tz = ZoneInfo("Asia/Dubai")
        self.start = datetime(2026, 11, 5, 21, 0, tzinfo=self.tz)
        self.moment = moments.Moment(
            key="ev1@x", kind="meeting", title="Interview with Acme", start=self.start,
            end=self.start.replace(minute=30), people=[{"name": "Kim", "email": "kim@acme.com"}],
            location="https://acme.zoom.us/j/111")
        self.found = {"email": "Kim: we'll cover your resume", "commitments": [], "people": []}

    def _run(self, verdict, at=None, found=None, state=None):
        import moments
        mon = engine.MomentMonitor(now_fn=lambda: at or self.start.replace(hour=20, minute=5))
        llm = MagicMock()
        if isinstance(verdict, Exception):
            llm.invoke.side_effect = verdict
        else:
            llm.invoke.return_value = MagicMock(content=verdict)
        state = {} if state is None else state
        with patch.object(moments, "upcoming", return_value=[self.moment]), \
             patch.object(moments, "my_name", return_value="Sam"), \
             patch.object(moments, "gather", return_value=found or self.found), \
             patch.object(engine, "_get_llm", return_value=llm), \
             patch("instructions.render_for_prompt", return_value="  3. Never mention the Acme offer"):
            out = mon.check(state)
        return out, llm, state

    def test_heads_up_is_a_nudge_and_fires_once(self):
        verdict = '{"why": "resume focus", "send": true, "message": "Acme in 1h — they\'ll cover your resume."}'
        out, llm, state = self._run(verdict)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "nudge")
        self.assertTrue(out[0].text.startswith("📅 Acme in 1h"))
        prompt = llm.invoke.call_args[0][0]
        self.assertIn("Never mention the Acme offer", prompt)          # standing rules bind it
        self.assertIn("source of truth", prompt)
        again, llm2, _ = self._run(verdict, state=state)
        self.assertEqual(again, [])
        llm2.invoke.assert_not_called()

    def test_silence_is_remembered_too(self):
        out, _, state = self._run('{"why": "nothing new", "send": false, "message": ""}')
        self.assertEqual(out, [])
        self.assertIn("ev1@x:before", state["done"])

    def test_nothing_to_add_skips_the_model(self):
        self.moment.people = []
        out, llm, state = self._run('{"send": true, "message": "x"}',
                                    found={"email": "", "commitments": [], "people": []})
        self.assertEqual(out, [])
        llm.invoke.assert_not_called()
        self.assertIn("ev1@x:before", state["done"])

    def test_a_made_up_join_link_drops_the_message(self):
        out, _, state = self._run('{"send": true, "message": "Use https://acme.zoom.us/j/999"}')
        self.assertEqual(out, [])
        self.assertIn("ev1@x:before", state["done"])

    def test_model_failure_retries_then_gives_up(self):
        state = {}
        for _ in range(2):
            out, _, state = self._run(RuntimeError("overloaded"), state=state)
            self.assertEqual(out, [])
            self.assertNotIn("ev1@x:before", state["done"])
        self._run(RuntimeError("overloaded"), state=state)
        self.assertIn("ev1@x:before", state["done"])

    def test_outside_the_window_nothing_happens(self):
        out, llm, _ = self._run('{"send": true, "message": "x"}',
                                at=self.start.replace(hour=12))
        self.assertEqual(out, [])
        llm.invoke.assert_not_called()


class TestSharedGate(TempStateMixin, unittest.TestCase):
    """One budget and one memory of what was said, across every monitor."""

    def test_judgment_pings_share_a_daily_budget(self):
        nudges = [Notification(f"idea {i}", kind="nudge") for i in range(engine.NUDGE_BUDGET + 2)]
        reports = [Notification("⏰ Call Sam"), Notification("📬 Evening digest")]
        sent = []
        eng = ProactiveEngine([StubMonitor(nudges + reports)],
                              notify_fn=lambda t: sent.append(t) or True)
        eng.tick(now_monotonic=0, now=datetime(2026, 6, 10, 12, 0))
        self.assertEqual(sum(t.startswith("idea") for t in sent), engine.NUDGE_BUDGET)
        self.assertIn("⏰ Call Sam", sent)                     # reports always go through
        self.assertIn("📬 Evening digest", sent)

    def test_budget_resets_the_next_day(self):
        sent = []
        mon = StubMonitor([Notification("idea", kind="nudge")] * engine.NUDGE_BUDGET, interval_seconds=1)
        eng = ProactiveEngine([mon], notify_fn=lambda t: sent.append(t) or True)
        eng.tick(now_monotonic=0, now=datetime(2026, 6, 10, 12, 0))
        eng.tick(now_monotonic=10, now=datetime(2026, 6, 10, 13, 0))
        self.assertEqual(len(sent), engine.NUDGE_BUDGET)
        eng.tick(now_monotonic=20, now=datetime(2026, 6, 11, 12, 0))
        self.assertEqual(len(sent), 2 * engine.NUDGE_BUDGET)

    def test_every_judge_sees_what_was_already_said(self):
        eng = ProactiveEngine([StubMonitor([Notification("📅 Acme in 1h, link on the invite")])],
                              notify_fn=lambda t: True)
        eng.tick(now_monotonic=0, now=datetime.now())
        with patch("instructions.render_for_prompt", return_value="  1. Be brief"):
            rules, told = engine.judgment_context()
        self.assertIn("Be brief", rules)
        self.assertIn("Acme in 1h", told)

    def test_chase_reads_rules_and_the_told_log(self):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content='{"send": false, "message": "", "commitment_ids": []}')
        open_ = [{"id": 3, "description": "Reply to Kim", "kind": "reply_owed", "due_date": None}]
        with patch('skills.commitment_manager.get_open_commitments', return_value=open_), \
             patch('skills.commitment_manager.slip_counts', return_value={}), \
             patch.object(engine, '_get_llm', return_value=llm), \
             patch.object(engine, 'judgment_context', return_value=("  2. No Acme talk", "- told: hi")):
            ChaseMonitor(now_fn=lambda: datetime(2026, 6, 10, 14, 0)).check({})
        prompt = llm.invoke.call_args[0][0]
        self.assertIn("No Acme talk", prompt)
        self.assertIn("- told: hi", prompt)
