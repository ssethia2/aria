"""Phase-1 multi-tenancy: tenant context, guest toolset, and per-user memory isolation.

The actual ChromaDB add/search needs embeddings (network), so those paths are mocked —
we assert the routing (which collection) rather than real vector recall. Offline.
Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import patch, MagicMock, mock_open

import tenant
import memory
import agent_core


class _GuestCtx:
    """with _GuestCtx('alice'): … — sets/clears the tenant context safely."""
    def __init__(self, uid):
        self.uid = uid
    def __enter__(self):
        self.tok = tenant.set_current_user(self.uid)
    def __exit__(self, *a):
        tenant.reset_current_user(self.tok)


class TestTenantContext(unittest.TestCase):
    def test_default_is_owner(self):
        self.assertIsNone(tenant.get_current_user())
        self.assertFalse(tenant.is_guest())

    def test_set_and_reset(self):
        with _GuestCtx("alice"):
            self.assertEqual(tenant.get_current_user(), "alice")
            self.assertTrue(tenant.is_guest())
        self.assertFalse(tenant.is_guest())          # restored after the block

    def test_safe_id(self):
        self.assertEqual(tenant.safe_id("alice@x.com"), "alice_x_com")
        self.assertEqual(tenant.safe_id(""), "anon")


class TestGuestToolset(unittest.TestCase):
    def test_guest_excludes_account_tools(self):
        names = {t.name for t in agent_core.build_tools(guest=True)}
        for keep in ("add_memory", "search_memory", "web_search", "fetch_webpage", "get_weather"):
            self.assertIn(keep, names)
        for forbidden in ("read_email_thread", "read_and_summarize_emails", "draft_email_reply",
                          "get_calendar_events", "create_calendar_event", "list_people",
                          "play_music", "control_light", "check_packages", "get_system_status",
                          "browse_and_report", "add_commitment", "create_note", "read_cold_storage"):
            self.assertNotIn(forbidden, names)

    def test_owner_toolset_is_full(self):
        names = {t.name for t in agent_core.build_tools(guest=False)}
        self.assertIn("read_email_thread", names)
        self.assertGreater(len(names), 30)


class TestGuestMemoryIsolation(unittest.TestCase):
    def _mocks(self, query_docs=None):
        client, col, emb = MagicMock(), MagicMock(), MagicMock()
        client.get_or_create_collection.return_value = col
        emb.embed_query.return_value = [0.1, 0.2]
        col.query.return_value = {'documents': [query_docs or []]}
        return client, col, emb

    def test_guest_add_routes_to_own_collection(self):
        client, col, emb = self._mocks()
        with _GuestCtx("bob"), patch.object(memory, 'chroma_client', client), \
             patch.object(memory, 'embeddings', emb):
            out = memory.add_memory.invoke({'fact': 'bob likes tea'})
        client.get_or_create_collection.assert_called_once_with(name='mem_bob')
        col.add.assert_called_once()
        self.assertIn('remember', out.lower())

    def test_owner_add_uses_scratchpad_not_chroma(self):
        client, _, emb = self._mocks()
        m = mock_open()
        with patch('builtins.open', m), patch.object(memory, 'chroma_client', client):
            out = memory.add_memory.invoke({'fact': 'owner fact'})
        client.get_or_create_collection.assert_not_called()   # owner never hits guest path
        m.assert_called()                                      # wrote the scratchpad
        self.assertIn('working memory', out.lower())

    def test_guest_search_queries_only_own_collection(self):
        client, col, emb = self._mocks(query_docs=['bob likes tea'])
        with _GuestCtx("bob"), patch.object(memory, 'chroma_client', client), \
             patch.object(memory, 'embeddings', emb):
            out = memory.search_memory.invoke({'query': 'drinks'})
        client.get_or_create_collection.assert_called_once_with(name='mem_bob')
        self.assertIn('bob likes tea', out)

    def test_guest_profile_is_empty(self):
        with _GuestCtx("bob"):
            self.assertEqual(memory.load_profile(), {})


if __name__ == '__main__':
    unittest.main()
