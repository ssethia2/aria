"""Tests for the Netflix skill — CTA extraction, page-state verification, honest reporting.

Gmail and Playwright are mocked; these run offline.
Run: python3 -m unittest discover tests
"""
import base64
import unittest
from unittest.mock import MagicMock, patch

from skills import netflix_manager


def _service_with_html(html: str):
    data = base64.urlsafe_b64encode(html.encode()).decode()
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value \
        .execute.return_value = {'messages': [{'id': 'n1'}]}
    service.users.return_value.messages.return_value.get.return_value \
        .execute.return_value = {
            'payload': {'mimeType': 'text/html', 'parts': [],
                        'body': {'data': data}}}
    return service


class TestLinkExtraction(unittest.TestCase):
    def test_prefers_cta_href_over_other_links(self):
        html = ('<a href="https://www.netflix.com/watch/123">Watch now</a>'
                '<a href="https://www.netflix.com/account/update-primary-location?t=abc">'
                'Update Netflix Household</a>')
        link = netflix_manager.extract_netflix_link(_service_with_html(html))
        self.assertIn('update-primary-location', link)

    def test_falls_back_to_anchor_text(self):
        html = '<a href="https://nflx.it/t/xyz">Yes, this was me</a>'
        link = netflix_manager.extract_netflix_link(_service_with_html(html))
        self.assertEqual(link, 'https://nflx.it/t/xyz')

    def test_no_blind_first_link_grab(self):
        """A random tracking link must NOT be returned (the /browse incident)."""
        html = ('<a href="https://www.netflix.com/browse?g=token">See what\'s new</a>'
                '<a href="https://netflix.com/unsubscribe">Unsubscribe</a>')
        self.assertIsNone(
            netflix_manager.extract_netflix_link(_service_with_html(html)))


class TestPageOutcomeClassifier(unittest.TestCase):
    def test_real_live_page_text_classifies_confirmed(self):
        """The actual post-click page observed on 2026-06-10."""
        self.assertEqual(netflix_manager.classify_page_outcome(
            "You've updated your Netflix Household\n"
            "Watch at home and on the go using your Netflix Household devices.",
            clicked=True), "confirmed")

    def test_explicit_confirmation_wins(self):
        self.assertEqual(netflix_manager.classify_page_outcome(
            "Great — your Netflix Household is updated. You're all set!", clicked=True),
            "confirmed")
        self.assertEqual(netflix_manager.classify_page_outcome(
            "Your Netflix Household is updated.", clicked=False), "confirmed")

    def test_expired_link_detected(self):
        self.assertEqual(netflix_manager.classify_page_outcome(
            "This link has expired. Request a new link.", clicked=False), "expired")

    def test_login_wall_detected(self):
        self.assertEqual(netflix_manager.classify_page_outcome(
            "Sign In to Netflix. Email or phone number. Password.", clicked=False),
            "login_required")

    def test_ambiguous_pages_stay_honest(self):
        self.assertEqual(netflix_manager.classify_page_outcome(
            "Welcome to Netflix.", clicked=True), "clicked")
        self.assertEqual(netflix_manager.classify_page_outcome(
            "Welcome to Netflix.", clicked=False), "visited")
        self.assertEqual(netflix_manager.classify_page_outcome("", clicked=False), "visited")


class TestHonestOutcomeReporting(unittest.TestCase):
    """The tool's message must never claim more certainty than was observed."""

    def _run_tool(self, click_outcome):
        with patch.object(netflix_manager, 'get_netflix_gmail_service',
                          return_value=MagicMock()), \
             patch.object(netflix_manager, 'extract_netflix_link',
                          return_value='https://netflix.com/x'), \
             patch.object(netflix_manager, 'click_netflix_update',
                          return_value=click_outcome):
            return netflix_manager.update_netflix_household.invoke({})

    def test_confirmed_reports_verified(self):
        msg = self._run_tool("confirmed")
        self.assertIn("Verified", msg)

    def test_clicked_notes_missing_confirmation(self):
        msg = self._run_tool("clicked")
        self.assertIn("Clicked the confirmation button", msg)
        self.assertIn("didn't show an explicit confirmation", msg)

    def test_visited_admits_uncertainty(self):
        msg = self._run_tool("visited")
        self.assertIn("no confirmation button", msg)
        self.assertIn("can't verify", msg)
        self.assertNotIn("Verified", msg)

    def test_login_required_explains_limit(self):
        msg = self._run_tool("login_required")
        self.assertIn("login wall", msg)

    def test_expired_suggests_fresh_email(self):
        msg = self._run_tool("expired")
        self.assertIn("expired", msg)

    def test_failed_reports_failure(self):
        msg = self._run_tool("failed")
        self.assertIn("Failed", msg)

    def test_no_email_or_link_found(self):
        with patch.object(netflix_manager, 'get_netflix_gmail_service',
                          return_value=MagicMock()), \
             patch.object(netflix_manager, 'extract_netflix_link', return_value=None):
            msg = netflix_manager.update_netflix_household.invoke({})
        self.assertIn("Couldn't find", msg)


if __name__ == '__main__':
    unittest.main()
