"""Tests for the daily Markdown briefing renderer.

Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from ops import report_generator


SAMPLE_EMAILS = [
    {'id': '1', 'subject': 'Project deadline', 'sender': 'boss@work.com', 'snippet': '...'},
    {'id': '2', 'subject': 'BUY NOW 50% OFF', 'sender': 'spam@shop.com', 'snippet': '...'},
]
SAMPLE_CLASSIFICATIONS = [
    {'id': '1', 'category': 'IMPORTANT', 'summary': 'Deadline moved up.', 'action': 'KEEP'},
    {'id': '2', 'category': 'JUNK/PROMOTION', 'summary': '', 'action': 'DELETE'},
]
SAMPLE_NEWS = [{'headline': 'Big Event', 'summary': 'Something happened.', 'sources': ['NYT']}]
SAMPLE_REMINDERS = [{'id': 1, 'task': 'Call Mom', 'date': '2026-06-10'}]


class TestGenerateDailyMarkdown(unittest.TestCase):
    def test_full_report_contains_all_sections(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(report_generator, 'REPORT_DIR', tmp):
            path, content = report_generator.generate_daily_markdown(
                SAMPLE_CLASSIFICATIONS, SAMPLE_EMAILS, SAMPLE_NEWS, SAMPLE_REMINDERS)
            self.assertTrue(os.path.exists(path))

        self.assertIn('Call Mom', content)                  # reminders
        self.assertIn('Big Event', content)                 # news brief
        self.assertIn('Project deadline', content)          # important email
        self.assertIn('Deadline moved up.', content)        # its summary
        self.assertIn('BUY NOW 50% OFF', content)           # listed under cleaned-up
        self.assertIn('1 emails were labeled for deletion', content)

    def test_empty_inputs_still_render(self):
        """The empty-morning path must produce a valid report, not crash."""
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(report_generator, 'REPORT_DIR', tmp):
            path, content = report_generator.generate_daily_markdown([], [], None, None)
        self.assertIn('Daily Assistant Summary', content)
        self.assertIn('*None today.*', content)


if __name__ == '__main__':
    unittest.main()
