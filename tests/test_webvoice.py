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
             patch.object(server, "_agent_instance", return_value=agent), \
             patch.object(server, "quick_answer", return_value=None), \
             patch("webvoice.usage.allow", return_value=True), \
             patch("webvoice.usage.record"):
            r = self.client.post("/agent", json={"request": "remember I like tea", "token": "tok-alice"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], "hi alice")
        self.assertTrue(captured["guest"])                 # ran in guest mode
        self.assertEqual(captured["user"], "alice")        # as the right friend
        self.assertEqual(captured["thread"], "guest-alice")  # in their own thread
        self.assertFalse(tenant.is_guest())                # context cleaned up after

    def test_live_token_over_daily_cap_returns_429(self):
        with patch.object(server, "_friends", return_value={"good": "alice"}), \
             patch("webvoice.usage.check_and_increment", return_value=False):
            r = self.client.get("/live-token?t=good")
        self.assertEqual(r.status_code, 429)

    def test_agent_over_daily_cap_returns_friendly_message(self):
        # allow() False for every kind → quick path skipped, full-brain gate refuses.
        with patch.object(server, "_friends", return_value={"good": "alice"}), \
             patch("webvoice.usage.allow", return_value=False):
            r = self.client.post("/agent", json={"request": "hi", "token": "good"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("limit", r.json()["result"].lower())

    def test_text_uses_quick_path_and_skips_full_brain(self):
        # A pure general-knowledge question is answered by the cheap light tier; the full
        # (expensive) brain never runs, and the spend is charged as 'quick', not 'agent'.
        brain = MagicMock()
        brain.invoke.side_effect = AssertionError("full brain must not run on a quick hit")
        with patch.object(server, "_friends", return_value={"tk": "alice"}), \
             patch.object(server, "_agent_instance", return_value=brain), \
             patch.object(server, "quick_answer", return_value="Paris."), \
             patch("webvoice.usage.allow", return_value=True), \
             patch("webvoice.usage.record") as rec:
            r = self.client.post("/agent", json={"request": "capital of France?", "token": "tk"})
        self.assertEqual(r.json()["result"], "Paris.")
        rec.assert_called_once_with("alice", "quick")
        brain.invoke.assert_not_called()

    def test_name_record_threads_name_into_context(self):
        captured = {}

        def fake_invoke(payload, config=None):
            captured["name"] = tenant.get_current_name()
            captured["user"] = tenant.get_current_user()
            return {"messages": [MagicMock(content="ok")]}

        agent = MagicMock(); agent.invoke = fake_invoke
        with patch.object(server, "_friends", return_value={"tk": {"id": "alice", "name": "Alice"}}), \
             patch.object(server, "_agent_instance", return_value=agent), \
             patch.object(server, "quick_answer", return_value=None), \
             patch("webvoice.usage.allow", return_value=True), \
             patch("webvoice.usage.record"):
            r = self.client.post("/agent", json={"request": "hi", "token": "tk"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["user"], "alice")
        self.assertEqual(captured["name"], "Alice")

    def test_owner_token_runs_full_agent_no_caps(self):
        captured = {}

        def fake_invoke(payload, config=None):
            captured["thread"] = config["configurable"]["thread_id"]
            captured["guest"] = tenant.is_guest()      # owner mode must NOT be a guest tenant
            return {"messages": [MagicMock(content="full aria")]}

        owner = MagicMock(); owner.invoke = fake_invoke
        with patch.object(server, "OWNER_TOKEN", "secret"), \
             patch.object(server, "_owner_agent_instance", return_value=owner), \
             patch("webvoice.usage.allow") as allow, patch("webvoice.usage.record") as rec:
            r = self.client.post("/agent", json={"request": "read my email", "token": "secret"})
        self.assertEqual(r.json()["result"], "full aria")
        self.assertEqual(captured["thread"], "owner-web")
        self.assertFalse(captured["guest"])            # full owner, not the guest sandbox
        allow.assert_not_called(); rec.assert_not_called()   # caps bypassed for the owner

    def test_owner_mode_off_by_default(self):
        # With no ARIA_OWNER_TOKEN set, that token is just an invalid invite -> 403.
        with patch.object(server, "OWNER_TOKEN", None), \
             patch.object(server, "_friends", return_value={"good": "alice"}):
            self.assertEqual(self.client.post("/agent", json={"request": "hi", "token": "secret"}).status_code, 403)

    def test_user_for_resolves_and_raises(self):
        with patch.object(server, "_friends", return_value={"t": "bob"}):
            self.assertEqual(server._user_for("t"), "bob")
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                server._user_for("nope")


class TestVoiceTurn(unittest.TestCase):
    """Push-to-talk /voice-turn: the free STT->brain->TTS path."""

    def setUp(self):
        self.client = TestClient(server.app)

    def test_bad_token_rejected_before_any_work(self):
        with patch.object(server, "OWNER_TOKEN", None), \
             patch.object(server, "_friends", return_value={}), \
             patch("llm_router.transcribe_audio") as stt:
            r = self.client.post("/voice-turn?t=bad",
                                 content=b"x" * 500,
                                 headers={"Content-Type": "audio/mp4"})
        self.assertEqual(r.status_code, 403)
        stt.assert_not_called()          # no CPU spent on strangers

    def test_owner_roundtrip_transcript_reply_audio(self):
        with patch.object(server, "OWNER_TOKEN", "sec"), \
             patch("llm_router.transcribe_audio", return_value="how many commitments"), \
             patch.object(server, "agent", return_value={"result": "Eleven."}) as brain, \
             patch.object(server, "_tts_m4a", return_value=b"FAKEAAC"):
            r = self.client.post("/voice-turn?t=sec",
                                 content=b"x" * 500,
                                 headers={"Content-Type": "audio/mp4"})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["you"], "how many commitments")
        self.assertEqual(j["reply"], "Eleven.")
        import base64 as b64
        self.assertEqual(b64.b64decode(j["audio_b64"]), b"FAKEAAC")
        self.assertEqual(brain.call_args[0][0].request, "how many commitments")
        self.assertEqual(brain.call_args[0][0].token, "sec")

    def test_unintelligible_audio_returns_friendly_retry(self):
        with patch.object(server, "OWNER_TOKEN", "sec"), \
             patch("llm_router.transcribe_audio", return_value="  "), \
             patch.object(server, "agent") as brain:
            r = self.client.post("/voice-turn?t=sec",
                                 content=b"x" * 500,
                                 headers={"Content-Type": "audio/mp4"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("try again", r.json()["reply"].lower())
        brain.assert_not_called()

    def test_tts_failure_still_returns_text(self):
        with patch.object(server, "OWNER_TOKEN", "sec"), \
             patch("llm_router.transcribe_audio", return_value="hello"), \
             patch.object(server, "agent", return_value={"result": "Hi!"}), \
             patch.object(server, "_tts_m4a", return_value=None):
            r = self.client.post("/voice-turn?t=sec",
                                 content=b"x" * 500,
                                 headers={"Content-Type": "audio/mp4"})
        j = r.json()
        self.assertEqual(j["reply"], "Hi!")
        self.assertIsNone(j["audio_b64"])    # voice degraded, text intact

    def test_tiny_body_rejected(self):
        with patch.object(server, "OWNER_TOKEN", "sec"):
            r = self.client.post("/voice-turn?t=sec", content=b"xx",
                                 headers={"Content-Type": "audio/mp4"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
