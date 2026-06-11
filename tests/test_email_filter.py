"""Tests for the deterministic bulk-mail pre-filter.

Run: python3 -m unittest discover tests
"""
import unittest

from email_filter import is_bulk


class TestIsBulk(unittest.TestCase):
    def test_list_unsubscribe_is_bulk(self):
        self.assertTrue(is_bulk({'sender': 'Morning Brew', 'subject': 'Daily',
                                 'list_unsubscribe': '<https://unsub.example.com>'}))

    def test_no_unsubscribe_is_not_bulk(self):
        # A real person, or transactional mail (bill/security/travel) — must reach the LLM.
        self.assertFalse(is_bulk({'sender': 'Rohan <rohan@x.com>', 'subject': 'Trip?'}))
        self.assertFalse(is_bulk({'sender': 'no-reply@bank.com', 'subject': 'Security alert',
                                  'list_unsubscribe': ''}))

    def test_whitespace_only_header_not_bulk(self):
        self.assertFalse(is_bulk({'list_unsubscribe': '   '}))


if __name__ == '__main__':
    unittest.main()
