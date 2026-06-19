"""Web push for guest reminders: subscribe routing + the per-tenant ping scheduler.
Offline — pywebpush send is mocked; uses temp files + a temp commitments DB.

Run: python3 -m unittest discover tests
"""
import os
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

import tenant
from webvoice import server, push


class TestSubscribeRouting(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_key_endpoint_returns_public_key(self):
        with patch.object(push, "public_key", return_value="PUBKEY"):
            self.assertEqual(self.client.get("/push/key").json()["key"], "PUBKEY")

    def test_guest_subscription_stored_under_their_id(self):
        sub = {"endpoint": "https://push/abc", "keys": {}}
        with patch.object(server, "_friends", return_value={"tok": "alice"}), \
             patch.object(push, "add_subscription") as add:
            r = self.client.post("/push/subscribe", json={"token": "tok", "subscription": sub})
        self.assertEqual(r.status_code, 200)
        add.assert_called_once_with("alice", sub)

    def test_owner_subscription_stored_under_owner(self):
        sub = {"endpoint": "https://push/xyz", "keys": {}}
        with patch.object(server, "OWNER_TOKEN", "secret"), \
             patch.object(push, "add_subscription") as add:
            self.client.post("/push/subscribe", json={"token": "secret", "subscription": sub})
        add.assert_called_once_with("owner", sub)

    def test_bad_token_subscription_rejected(self):
        with patch.object(server, "OWNER_TOKEN", None), \
             patch.object(server, "_friends", return_value={"good": "alice"}):
            r = self.client.post("/push/subscribe", json={"token": "bad", "subscription": {}})
        self.assertEqual(r.status_code, 403)


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        p = Path(self.d.name)
        self._subs = patch.object(push, "SUBS_PATH", p / "subs.json"); self._subs.start()
        self._state = patch.object(push, "STATE_PATH", p / "state.json"); self._state.start()
        from skills import commitment_manager as cm
        self.cm = cm
        self.db = patch.object(cm, "DB_PATH", str(p / "cal.db")); self.db.start()
        cm.init_db()
        push.add_subscription("alice", {"endpoint": "https://push/alice"})

    def tearDown(self):
        self._subs.stop(); self._state.stop(); self.db.stop(); self.d.cleanup()

    def test_due_reminder_pushes_once_then_dedupes(self):
        now = datetime(2026, 6, 20, 12, 0)
        today = now.strftime("%Y-%m-%d")
        tok = tenant.set_current_user("alice")
        try:
            self.cm.add("call mom", due_date=today, due_time="00:00")
        finally:
            tenant.reset_current_user(tok)

        with patch.object(push, "send") as send:
            push._tick(now)
            push._tick(now)        # second pass same minute: must NOT re-push
        send.assert_called_once()
        self.assertEqual(send.call_args[0][0], "alice")          # pushed to alice
        self.assertIn("call mom", send.call_args[0][2])          # with the reminder text

    def test_no_subscription_no_push(self):
        now = datetime(2026, 6, 20, 12, 0)
        tok = tenant.set_current_user("bob")     # bob has a due item but never subscribed
        try:
            self.cm.add("dentist", due_date=now.strftime("%Y-%m-%d"), due_time="00:00")
        finally:
            tenant.reset_current_user(tok)
        with patch.object(push, "send") as send:
            push._tick(now)
        send.assert_not_called()                 # scheduler only visits subscribed users


if __name__ == "__main__":
    unittest.main()
