"""Tests for situation.py: the mail and calendar around open commitments. No network."""
import unittest
from unittest.mock import MagicMock, patch

import situation


def item(cid, description, who=None):
    return {"id": cid, "description": description, "who": who, "recurring": None,
            "created_at": "2026-11-03 10:00:00"}


class TestSearchTerms(unittest.TestCase):
    def test_person_first_then_words_only_this_item_has(self):
        items = [item(1, "Schedule Acme interview", who="Kim"),
                 item(2, "Prepare for Northwind interview")]
        self.assertEqual(situation.search_terms(items[0], items), ["kim", "acme"])
        self.assertNotIn("interview", situation.search_terms(items[1], items))


class TestRender(unittest.TestCase):
    def test_marks_his_own_mail_and_lists_the_calendar(self):
        sit = {"calendar": ["Thu Nov 12, 2:00 PM — Acme interview"],
               "mail": {1: [{"id": "a", "direction": "sent", "who": "Kim <kim@acme.example>",
                             "subject": "Re: Interview slots", "snippet": "Thursday 2pm works",
                             "ts": 1794000000}]}}
        text = situation.render([item(1, "Schedule Acme interview")], sit)
        self.assertIn("HE SENT to Kim", text)
        self.assertIn("Re: Interview slots", text)
        self.assertIn("Thu Nov 12, 2:00 PM — Acme interview", text)

    def test_nothing_found_renders_empty(self):
        self.assertEqual(situation.render([item(1, "x y z")], {"calendar": [], "mail": {}}), "")


class TestAround(unittest.TestCase):
    def setUp(self):
        situation._cache.clear()

    def test_failures_degrade_to_nothing(self):
        with patch("skills.google_calendar.fetch_events", side_effect=RuntimeError("down")), \
             patch("skills.email_manager.get_gmail_service", side_effect=RuntimeError("down")):
            self.assertEqual(situation.around([item(1, "Schedule Acme interview")]),
                             {"calendar": [], "mail": {}})

    def test_best_matches_kept_oldest_first(self):
        service = MagicMock()
        msgs = service.users.return_value.messages.return_value
        payloads = {
            "new": {"labelIds": ["SENT"], "internalDate": "2000000", "snippet": "Thursday works",
                    "payload": {"headers": [{"name": "To", "value": "kim@acme.example"},
                                            {"name": "Subject", "value": "Re: Acme interview"}]}},
            "old": {"labelIds": ["INBOX"], "internalDate": "1000000", "snippet": "pick a slot",
                    "payload": {"headers": [{"name": "From", "value": "Kim <kim@acme.example>"},
                                            {"name": "Subject", "value": "Acme interview"}]}}}

        listing = {"messages": [{"id": "new"}, {"id": "old"}]}

        def new_batch(callback):
            batch, queued = MagicMock(), []
            batch.add.side_effect = lambda req, request_id: queued.append(request_id)
            batch.execute.side_effect = lambda: [
                callback(k, payloads.get(k, listing), None) for k in queued]
            return batch
        service.new_batch_http_request.side_effect = new_batch
        mail = situation._mail_for(service, {7: ["acme"]})[7]
        self.assertEqual([m["id"] for m in mail], ["old", "new"])
        self.assertEqual(mail[1]["direction"], "sent")
        self.assertIn("newer_than:21d {acme}", msgs.list.call_args.kwargs["q"])


if __name__ == "__main__":
    unittest.main()
