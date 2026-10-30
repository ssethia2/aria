"""Tests for the email path — the code that touches the real inbox.

All Gmail/GCS/LLM access is mocked; these run offline.
Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

from skills import email_manager


class TestRunEmailSummary(unittest.TestCase):
    def test_no_emails_returns_two_empty_lists(self):
        """Callers unpack a 2-tuple; the empty-inbox path must honor that contract."""
        with patch.object(email_manager, 'get_gmail_service', return_value=MagicMock()), \
             patch.object(email_manager, 'fetch_recent_emails', return_value=[]):
            result = email_manager.run_email_summary()
        self.assertEqual(result, ([], []))

    def test_no_service_returns_two_falsy_values(self):
        with patch.object(email_manager, 'get_gmail_service', return_value=None):
            classifications, raw_emails = email_manager.run_email_summary()
        self.assertFalse(classifications)
        self.assertFalse(raw_emails)


class TestSendEmailAllowlist(unittest.TestCase):
    def test_blocks_recipient_not_on_allowlist(self):
        service = MagicMock()
        with patch.object(email_manager, 'get_allowed_recipients',
                          return_value=['me@example.com']):
            result = email_manager.send_email(service, 'attacker@evil.com', 'subj', 'body')
        self.assertIsNone(result)
        service.users.assert_not_called()  # never even touched the Gmail API

    def test_blocks_everyone_when_allowlist_empty(self):
        """Fail-safe-empty: if the allowlist can't load, nothing sends (ADR 0003)."""
        service = MagicMock()
        with patch.object(email_manager, 'get_allowed_recipients', return_value=[]):
            result = email_manager.send_email(service, 'me@example.com', 'subj', 'body')
        self.assertIsNone(result)
        service.users.assert_not_called()

    def test_sends_to_allowlisted_recipient(self):
        service = MagicMock()
        send_call = service.users.return_value.messages.return_value.send
        send_call.return_value.execute.return_value = {'id': 'msg-123'}
        with patch.object(email_manager, 'get_allowed_recipients',
                          return_value=['me@example.com']):
            result = email_manager.send_email(service, 'me@example.com', 'subj', 'body')
        self.assertEqual(result, {'id': 'msg-123'})
        self.assertTrue(send_call.called)


class TestDraftReply(unittest.TestCase):
    def _service(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value \
            .execute.return_value = {'messages': [{'id': 'm1'}]}
        service.users.return_value.messages.return_value.get.return_value \
            .execute.return_value = {
                'threadId': 'thread9',
                'payload': {'headers': [
                    {'name': 'From', 'value': 'Rohan <rohan@x.com>'},
                    {'name': 'Subject', 'value': 'Trip plans?'},
                    {'name': 'Message-ID', 'value': '<abc@mail>'}]}}
        return service

    def test_creates_threaded_draft_and_sends_nothing(self):
        service = self._service()
        with patch.object(email_manager, 'get_gmail_service', return_value=service):
            msg = email_manager.draft_email_reply.invoke({
                'search_query': 'from:rohan trip', 'body': 'Listings coming Friday!'})
        draft_call = service.users.return_value.drafts.return_value.create
        draft_call.assert_called_once()
        self.assertEqual(draft_call.call_args.kwargs['body']['message']['threadId'], 'thread9')
        service.users.return_value.messages.return_value.send.assert_not_called()
        self.assertIn('Nothing was sent', msg)

    def test_no_match_reports_cleanly(self):
        service = self._service()
        service.users.return_value.messages.return_value.list.return_value \
            .execute.return_value = {}
        with patch.object(email_manager, 'get_gmail_service', return_value=service):
            msg = email_manager.draft_email_reply.invoke({
                'search_query': 'from:nobody', 'body': 'x'})
        self.assertIn("Couldn't find", msg)

    def test_search_skips_arias_own_briefing(self):
        service = self._service()
        with patch.object(email_manager, 'get_gmail_service', return_value=service):
            email_manager.draft_email_reply.invoke({'search_query': 'acme', 'body': 'x'})
        q = service.users.return_value.messages.return_value.list.call_args.kwargs['q']
        self.assertEqual(q, '(acme) -subject:"Daily Assistant Summary"')


class TestAllowlistFailSafe(unittest.TestCase):
    def test_gcs_failure_falls_back_to_local_allowlist(self):
        """GCS unreachable + allow.json present → local list is used (ADR 0005)."""
        with patch.object(email_manager, '_cached_allowlist', None), \
             patch.dict('os.environ', {'ALLOWLIST_BUCKET_NAME': 'some-bucket'}), \
             patch('google.auth.default', side_effect=Exception('GCS down')), \
             patch.object(email_manager, '_load_local_allowlist',
                          return_value=['me@example.com']):
            result = email_manager.get_allowed_recipients()
        self.assertEqual(result, ['me@example.com'])

    def test_no_source_at_all_returns_empty_list(self):
        """GCS down AND no allow.json → EMPTY allowlist, never a permissive default."""
        with patch.object(email_manager, '_cached_allowlist', None), \
             patch.dict('os.environ', {'ALLOWLIST_BUCKET_NAME': 'some-bucket'}), \
             patch('google.auth.default', side_effect=Exception('GCS down')), \
             patch.object(email_manager, '_load_local_allowlist', return_value=None):
            result = email_manager.get_allowed_recipients()
        self.assertEqual(result, [])

    def test_no_bucket_configured_uses_local_allowlist(self):
        with patch.object(email_manager, '_cached_allowlist', None), \
             patch.dict('os.environ', {'ALLOWLIST_BUCKET_NAME': ''}), \
             patch.object(email_manager, '_load_local_allowlist',
                          return_value=['me@example.com']):
            result = email_manager.get_allowed_recipients()
        self.assertEqual(result, ['me@example.com'])

    def test_user_email_is_allowlist_fallback_without_allow_json(self):
        """Self-host convenience: USER_EMAIL stands in for allow.json."""
        import os
        allow_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(email_manager.__file__))), 'allow.json')
        with patch.dict('os.environ', {'USER_EMAIL': 'satvik@example.com'}), \
             patch('builtins.open', side_effect=FileNotFoundError):
            result = email_manager._load_local_allowlist()
        self.assertEqual(result, ['satvik@example.com'])


def _b64(text):
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode()


def _gmail_msg(sender, subject, text):
    return {'payload': {
        'headers': [{'name': 'From', 'value': sender},
                    {'name': 'Subject', 'value': subject},
                    {'name': 'Date', 'value': 'Mon, 1 Jan 2026'}],
        'mimeType': 'text/plain', 'body': {'data': _b64(text)}}}


class TestReadEmailThread(unittest.TestCase):
    def _service(self, threads, messages_by_thread):
        """threads: the threads.list hits; messages_by_thread: thread id -> its messages."""
        svc = MagicMock()
        api = svc.users.return_value.threads.return_value
        api.list.return_value.execute.return_value = {'threads': threads}
        api.get.side_effect = lambda **kw: MagicMock(
            execute=MagicMock(return_value={'messages': messages_by_thread[kw['id']]}))
        return svc

    def _read(self, svc, query):
        with patch('email_backend.using_app_password', return_value=False), \
             patch.object(email_manager, 'get_gmail_service', return_value=svc):
            return email_manager.read_email_thread.invoke({'search_query': query})

    def test_returns_thread_contents(self):
        svc = self._service([{'id': 't1', 'snippet': 'We should book'}], {'t1': [
            _gmail_msg('Rohan <r@x.com>', 'Project X', 'We should book flights for July.')]})
        out = self._read(svc, 'project x')
        self.assertIn('Rohan', out)
        self.assertIn('Project X', out)
        self.assertIn('book flights', out)

    def test_lists_every_match_not_just_the_newest(self):
        """The assessment miss: the search returned only the newest thread (an interview
        invite), so she never saw the test platform's 'thanks for taking' email one result
        down."""
        svc = self._service(
            [{'id': 't1', 'snippet': 'excited to move forward with the interview'},
             {'id': 't2', 'snippet': 'Thanks for completing <b>Acme</b> Hiring Test'}],
            {'t1': [_gmail_msg('Mike <m@acme.com>', 'Interview Availability Request', 'Pick a time.')],
             't2': [_gmail_msg('Acme Hiring <tests@assess.example.com>',
                               'Thanks for taking the Acme Hiring Test', 'Submission confirmed.')]})
        out = self._read(svc, 'acme')
        self.assertIn('1. ', out)
        self.assertIn('2. ', out)
        self.assertIn('Thanks for taking the Acme Hiring Test', out)
        self.assertIn('Thanks for completing Acme Hiring Test', out)   # highlight tags stripped
        self.assertIn('Pick a time.', out)                           # newest read in full
        self.assertNotIn('Submission confirmed.', out)              # the rest: snippet only

    def test_search_skips_arias_own_briefing_unless_its_the_only_match(self):
        briefing = [{'id': 'b1', 'snippet': '#213 Acme coding assessment'}]
        svc = self._service([], {'b1': [_gmail_msg('Me <me@x.com>', 'Your Daily Assistant Summary', 'x')]})
        api = svc.users.return_value.threads.return_value
        api.list.return_value.execute.side_effect = [{}, {'threads': briefing}]
        out = self._read(svc, 'acme assessment')
        queries = [c.kwargs['q'] for c in api.list.call_args_list]
        self.assertEqual(queries, ['(acme assessment) -subject:"Daily Assistant Summary"',
                                   'acme assessment'])
        self.assertIn('Daily Assistant Summary', out)

    def test_not_found_does_not_claim_no_access(self):
        out = self._read(self._service([], {}), 'nope')
        self.assertIn('no matching', out.lower())
        self.assertNotIn("can't access", out.lower())

    def test_no_service(self):
        with patch('email_backend.using_app_password', return_value=False), \
             patch.object(email_manager, 'get_gmail_service', return_value=None):
            out = email_manager.read_email_thread.invoke({'search_query': 'x'})
        self.assertIn("isn't available", out)

    def test_body_text_prefers_plain_then_strips_html(self):
        multipart = {'mimeType': 'multipart/alternative', 'body': {}, 'parts': [
            {'mimeType': 'text/plain', 'body': {'data': _b64('plain body')}},
            {'mimeType': 'text/html', 'body': {'data': _b64('<p>html body</p>')}}]}
        self.assertEqual(email_manager._body_text(multipart), 'plain body')
        html_only = {'mimeType': 'text/html', 'body': {'data': _b64('<b>Hello</b> there')}}
        self.assertIn('Hello', email_manager._body_text(html_only))


if __name__ == '__main__':
    unittest.main()
