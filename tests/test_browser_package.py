"""Tests for the agentic browser safety guards and package tracking.

The browser's payment/password guards are safety-critical — tested as pure logic.
Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

from skills import browser_manager as bm
from skills import package_manager as pm


class TestBrowserSafety(unittest.TestCase):
    def test_forbidden_labels_blocked(self):
        for label in ["Place Order", "Pay now", "Buy Now", "Complete Purchase",
                      "Confirm and Pay", "Place your order", "PAY $42.00"]:
            self.assertTrue(bm._is_forbidden(label), f"{label!r} should be blocked")

    def test_safe_labels_allowed(self):
        for label in ["Continue", "View cart", "Select meal", "Next", "See options",
                      "Add to cart", "Enter last name"]:
            self.assertFalse(bm._is_forbidden(label), f"{label!r} should be allowed")

    def test_finish_action_returns_report(self):
        page = MagicMock()
        page.url = 'https://airline.test/order'
        page.evaluate.return_value = {'text': 'Meal options', 'elements': []}
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=
            '{"action":"finish","report":"Options: chicken or pasta. Finish at the link."}')
        out = bm._drive(page, "find meals", None, llm)
        self.assertIn("chicken or pasta", out)

    def test_clicking_payment_button_stops_with_link(self):
        page = MagicMock()
        page.url = 'https://shop.test/checkout'
        page.evaluate.return_value = {
            'text': 'Review', 'elements': [{'idx': 0, 'tag': 'button', 'type': '', 'label': 'Place order'}]}
        target = MagicMock()
        target.inner_text.return_value = 'Place order'
        page.query_selector.return_value = target
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content='{"action":"click","index":0}')
        out = bm._drive(page, "buy it", None, llm)
        self.assertIn("stopped", out.lower())
        self.assertIn("shop.test/checkout", out)
        target.click.assert_not_called()          # the dangerous click never happened

    def test_password_field_refused(self):
        page = MagicMock()
        page.url = 'https://x.test/login'
        page.evaluate.return_value = {
            'text': 'Login', 'elements': [{'idx': 0, 'tag': 'input', 'type': 'password', 'label': 'Password'}]}
        target = MagicMock()
        target.get_attribute.return_value = 'password'
        page.query_selector.return_value = target
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content='{"action":"type","index":0,"value":"hunter2"}')
        out = bm._drive(page, "log in", None, llm)
        self.assertIn("password", out.lower())
        target.fill.assert_not_called()


class TestPackages(unittest.TestCase):
    def _service(self, subject, snippet=''):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value \
            .execute.return_value = {'messages': [{'id': 'p1'}]}
        service.users.return_value.messages.return_value.get.return_value \
            .execute.return_value = {
                'snippet': snippet,
                'payload': {'headers': [
                    {'name': 'From', 'value': 'Amazon'},
                    {'name': 'Subject', 'value': subject},
                    {'name': 'Date', 'value': 'Wed, 11 Jun 2026 10:00'}]}}
        return service

    def test_extracts_ups_tracking_link(self):
        svc = self._service('Shipped', 'Tracking 1Z999AA10123456784 on its way')
        with patch.object(pm, 'get_gmail_service', return_value=svc):
            out = pm.check_packages.invoke({})
        self.assertIn('UPS', out)
        self.assertIn('ups.com/track', out)

    def test_no_shipping_emails(self):
        svc = MagicMock()
        svc.users.return_value.messages.return_value.list.return_value \
            .execute.return_value = {}
        with patch.object(pm, 'get_gmail_service', return_value=svc):
            out = pm.check_packages.invoke({})
        self.assertIn('No shipping', out)


if __name__ == '__main__':
    unittest.main()
