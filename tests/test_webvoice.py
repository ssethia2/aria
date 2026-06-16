"""Phase-2 webvoice: invite-token auth + per-friend guest isolation. Offline (agent mocked).

Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

import tenant
from webvoice import server


class TestWebvoiceAuth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_agent_rejects_missing_or_bad_token(self):
        with patch.object(server, "_friends", return_value={"good": "alice"}):
            self.assertEqual(self.client.post("/agent", json={"request": "hi", "token": "bad"}).status_code, 403)
            self.assertEqual(self.client.post("/agent", json={"request": "hi"}).status_code, 403)

    def test_live_token_is_invite_gated(self):
        with patch.object(server, "_friends", return_value={"good": "alice"}):
            self.assertEqual(self.client.get("/live-token?t=bad").status_code, 403)

    def test_valid_token_routes_into_isolated_guest_context(self):
        captured = {}

        def fake_invoke(payload, config=None):
            # assert the tenant context is set to THIS friend during the brain call
            captured["guest"] = tenant.is_guest()
            captured["user"] = tenant.get_current_user()
            captured["thread"] = config["configurable"]["thread_id"]
            return {"messages": [MagicMock(content="hi alice")]}

        agent = MagicMock(); agent.invoke = fake_invoke
        with patch.object(server, "_friends", return_value={"tok-alice": "alice"}), \
             patch.object(server, "_agent_instance", return_value=agent):
            r = self.client.post("/agent", json={"request": "remember I like tea", "token": "tok-alice"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], "hi alice")
        self.assertTrue(captured["guest"])                 # ran in guest mode
        self.assertEqual(captured["user"], "alice")        # as the right friend
        self.assertEqual(captured["thread"], "guest-alice")  # in their own thread
        self.assertFalse(tenant.is_guest())                # context cleaned up after

    def test_user_for_resolves_and_raises(self):
        with patch.object(server, "_friends", return_value={"t": "bob"}):
            self.assertEqual(server._user_for("t"), "bob")
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                server._user_for("nope")


if __name__ == "__main__":
    unittest.main()
