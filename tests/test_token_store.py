"""Per-tenant Gmail custody (ADR 0007): encrypted token store + principal-aware service.
Offline — no Google network. Run: python3 -m unittest discover tests
"""
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tenant
import token_store


class TestEncryptedStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self._dir = patch.object(token_store, "TOKENS_DIR", Path(self.d.name)); self._dir.start()
        self._env = patch.dict(os.environ, {"ARIA_TOKEN_ENC_KEY": token_store.generate_key()})
        self._env.start()

    def tearDown(self):
        self._dir.stop(); self._env.stop(); self.d.cleanup()

    def test_roundtrip(self):
        token_store.save("alice", {"refresh_token": "secret-abc"})
        self.assertTrue(token_store.has("alice"))
        self.assertEqual(token_store.load("alice")["refresh_token"], "secret-abc")

    def test_on_disk_is_encrypted_not_plaintext(self):
        token_store.save("alice", {"refresh_token": "secret-abc"})
        raw = (Path(self.d.name) / f"{tenant.safe_id('alice')}.gtok").read_bytes()
        self.assertNotIn(b"secret-abc", raw)        # ciphertext, not the token

    def test_tenants_isolated(self):
        token_store.save("alice", {"refresh_token": "A"})
        token_store.save("bob", {"refresh_token": "B"})
        self.assertEqual(token_store.load("bob")["refresh_token"], "B")
        self.assertIsNone(token_store.load("carol"))   # never connected

    def test_wrong_key_reads_as_not_connected(self):
        token_store.save("alice", {"refresh_token": "A"})
        with patch.dict(os.environ, {"ARIA_TOKEN_ENC_KEY": token_store.generate_key()}):
            self.assertIsNone(token_store.load("alice"))   # can't decrypt -> not connected

    def test_delete(self):
        token_store.save("alice", {"refresh_token": "A"})
        self.assertTrue(token_store.delete("alice"))
        self.assertFalse(token_store.has("alice"))
        self.assertFalse(token_store.delete("alice"))      # idempotent


class TestAtomicWrite(unittest.TestCase):
    def test_atomic_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "token.json")
            token_store.atomic_write_text(p, '{"a": 1}')
            self.assertEqual(json.load(open(p))["a"], 1)

    def test_failed_write_leaves_original_intact(self):
        # If the write blows up mid-serialize, the existing good file must survive (not be
        # truncated to empty) — the exact regression that 0-byte'd token.json.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "token.json")
            token_store.atomic_write_text(p, '{"good": true}')
            class Boom:
                def __str__(self): raise RuntimeError("serialize failed")
            with self.assertRaises(Exception):
                token_store.atomic_write_text(p, Boom())   # f.write(non-str) raises
            self.assertEqual(json.load(open(p))["good"], True)   # original preserved
            # no leftover temp files
            self.assertEqual([f for f in os.listdir(d) if f != "token.json"], [])


class TestPrincipalAwareGmail(unittest.TestCase):
    """get_gmail_service routes by principal and fails closed for guests."""

    def test_owner_path_does_not_touch_guest_store(self):
        from skills import email_manager
        # Owner (default principal): must NOT call the guest token store at all.
        with patch("skills.email_manager._guest_gmail_service") as guest, \
             patch("os.path.exists", return_value=False), \
             patch.object(email_manager, "InstalledAppFlow"):
            try:
                email_manager.get_gmail_service()
            except Exception:
                pass
        guest.assert_not_called()

    def test_guest_not_connected_returns_none_never_owner_token(self):
        from skills import email_manager
        tok = tenant.set_current_user("alice")
        try:
            with patch("token_store.load", return_value=None) as load:
                svc = email_manager.get_gmail_service()
        finally:
            tenant.reset_current_user(tok)
        self.assertIsNone(svc)              # fail-closed: no service, not owner's
        load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
