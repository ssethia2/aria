"""Tests for the IMAP/SMTP email backend (no-GCP-project path). Sockets mocked.

Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

from integrations import email_backend as eb


class TestModeSelection(unittest.TestCase):
    def test_app_password_mode_requires_both(self):
        with patch.dict('os.environ', {'EMAIL_APP_PASSWORD': 'abcd', 'USER_EMAIL': 'me@x.com'}):
            self.assertTrue(eb.using_app_password())
        with patch.dict('os.environ', {'EMAIL_APP_PASSWORD': 'abcd', 'USER_EMAIL': ''}):
            self.assertFalse(eb.using_app_password())
        with patch.dict('os.environ', {'EMAIL_APP_PASSWORD': '', 'USER_EMAIL': 'me@x.com'}):
            self.assertFalse(eb.using_app_password())

    def test_hosts_default_to_gmail(self):
        with patch.dict('os.environ', {'USER_EMAIL': 'me@gmail.com', 'EMAIL_APP_PASSWORD': 'x',
                                       'IMAP_HOST': '', 'SMTP_HOST': '', 'SMTP_PORT': ''}, clear=False):
            c = eb._cfg()
        self.assertEqual(c['imap_host'], 'imap.gmail.com')
        self.assertEqual(c['smtp_host'], 'smtp.gmail.com')
        self.assertEqual(c['smtp_port'], 587)


class TestParsing(unittest.TestCase):
    def test_parse_extracts_headers_and_snippet(self):
        raw = (b"From: Rohan <rohan@x.com>\r\nSubject: Trip plans?\r\n"
               b"Message-ID: <abc@mail>\r\nContent-Type: text/plain\r\n\r\n"
               b"Hey, any update on the trip?\r\n")
        d = eb._parse(raw)
        self.assertEqual(d['sender'], 'Rohan <rohan@x.com>')
        self.assertEqual(d['subject'], 'Trip plans?')
        self.assertEqual(d['id'], '<abc@mail>')
        self.assertIn('update on the trip', d['snippet'])


class TestSend(unittest.TestCase):
    def test_smtp_send_uses_starttls_and_login(self):
        smtp = MagicMock()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=smtp)
        cm.__exit__ = MagicMock(return_value=False)
        env = {'USER_EMAIL': 'me@gmail.com', 'EMAIL_APP_PASSWORD': 'app-pw'}
        with patch.dict('os.environ', env, clear=False), \
             patch.object(eb.smtplib, 'SMTP', return_value=cm):
            ok = eb.smtp_send('rohan@x.com', 'Re: Trip', 'On it!', in_reply_to='<abc@mail>')
        self.assertTrue(ok)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with('me@gmail.com', 'app-pw')
        smtp.send_message.assert_called_once()
        sent = smtp.send_message.call_args.args[0]
        self.assertEqual(sent['In-Reply-To'], '<abc@mail>')

    def test_draft_appends_to_a_drafts_folder(self):
        m = MagicMock()
        # first folder append succeeds
        m.append.return_value = ('OK', [b'done'])
        env = {'USER_EMAIL': 'me@gmail.com', 'EMAIL_APP_PASSWORD': 'app-pw'}
        with patch.dict('os.environ', env, clear=False), \
             patch.object(eb, '_imap', return_value=m):
            ok = eb.imap_create_draft('rohan@x.com', 'Re: Trip', 'draft body')
        self.assertTrue(ok)
        self.assertEqual(m.append.call_args.args[0], '[Gmail]/Drafts')


class TestFetch(unittest.TestCase):
    def test_fetch_recent_parses_messages_newest_first(self):
        m = MagicMock()
        m.search.return_value = ('OK', [b'1 2 3'])
        raw1 = b"From: A\r\nSubject: One\r\nMessage-ID: <1>\r\n\r\nbody one"
        raw3 = b"From: C\r\nSubject: Three\r\nMessage-ID: <3>\r\n\r\nbody three"
        m.fetch.side_effect = lambda i, spec: (
            ('OK', [(b'x', raw3)]) if i == b'3' else ('OK', [(b'x', raw1)]))
        with patch.object(eb, '_imap', return_value=m):
            out = eb.imap_fetch_recent(max_results=2)
        self.assertEqual(out[0]['subject'], 'Three')   # newest first
        self.assertEqual(len(out), 2)


if __name__ == '__main__':
    unittest.main()
