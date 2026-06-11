"""Tests for the Telegram notification channel.

Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

import notify


class TestSendTelegram(unittest.TestCase):
    def test_missing_config_returns_false_without_network(self):
        with patch.dict('os.environ',
                        {'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_ALLOWED_CHAT_ID': ''}), \
             patch.object(notify.requests, 'post') as post:
            self.assertFalse(notify.send_telegram('hello'))
        post.assert_not_called()

    def test_sends_to_all_allowlisted_chats(self):
        env = {'TELEGRAM_BOT_TOKEN': 'tok', 'TELEGRAM_ALLOWED_CHAT_ID': '111, 222'}
        with patch.dict('os.environ', env), \
             patch.object(notify.requests, 'post', return_value=MagicMock()) as post:
            self.assertTrue(notify.send_telegram('hello'))
        self.assertEqual(post.call_count, 2)

    def test_long_message_is_chunked(self):
        env = {'TELEGRAM_BOT_TOKEN': 'tok', 'TELEGRAM_ALLOWED_CHAT_ID': '111'}
        long_text = 'x' * (notify.TELEGRAM_MSG_LIMIT + 10)
        with patch.dict('os.environ', env), \
             patch.object(notify.requests, 'post', return_value=MagicMock()) as post:
            self.assertTrue(notify.send_telegram(long_text))
        self.assertEqual(post.call_count, 2)

    def test_network_failure_returns_false_not_raise(self):
        env = {'TELEGRAM_BOT_TOKEN': 'tok', 'TELEGRAM_ALLOWED_CHAT_ID': '111'}
        with patch.dict('os.environ', env), \
             patch.object(notify.requests, 'post', side_effect=Exception('net down')):
            self.assertFalse(notify.send_telegram('hello'))


if __name__ == '__main__':
    unittest.main()
