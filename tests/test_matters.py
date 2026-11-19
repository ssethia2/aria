"""Tests for matters.py: the picture of what is in motion, and how mail is folded into it.
No network; every store is a throwaway."""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import engine
import matters
from skills import commitment_manager as cm

NOW = datetime(2026, 11, 18, 10, 0)     # a Wednesday

MAIL = [
    {"id": "e1", "direction": "received", "from": "Sam Rivera <sam@acme.example>",
     "subject": "Acme next steps",
     "text": "Congrats, you passed the technical round! The onsite will be on December 3 "
             "in our Porto office. Can you confirm?"},
    {"id": "e2", "direction": "sent", "to": "Sam Rivera <sam@acme.example>",
     "subject": "Re: Acme next steps", "text": "Thanks Sam, I'll confirm by Friday."},
]


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._p = patch.object(cm, "DB_PATH", os.path.join(self.dir, "c.db"))
        self._p.start()
        cm.init_db()
        self._d = patch("decisions.note")
        self._d.start()
        self._me = patch("moments.my_name", return_value="Jo Park")
        self._me.start()

    def tearDown(self):
        self._me.stop()
        self._d.stop()
        self._p.stop()

    def fold(self, *changes, mail=MAIL, open_ids=(), calendar=""):
        return matters.apply(list(changes), mail, set(open_ids), NOW, calendar)


class TestStore(Store):
    def test_create_update_close_reopen_keep_a_history(self):
        mid = matters.create({"title": "Acme interview process", "stage": "applied"},
                             source="chat", now=NOW)
        self.assertEqual(matters.update(mid, {"stage": "applied"}, now=NOW), {})
        self.assertEqual(matters.update(mid, {"stage": "technical round"}, now=NOW),
                         {"stage": "technical round"})
        self.assertTrue(matters.close(mid, "withdrew", now=NOW))
        self.assertEqual(matters.open_matters(), [])
        self.assertEqual([m["id"] for m in matters.recently_closed(now=NOW)], [mid])
        self.assertTrue(matters.reopen(mid, now=NOW))
        self.assertEqual(len(matters.events(mid)), 4)

    def test_render_says_whose_it_is_and_whose_move(self):
        mid = matters.create({"title": "Flights to Lisbon", "owner": "Kim", "move": "theirs",
                              "next_step": "airline confirms the seat", "stage": "booked"},
                             items=[7], now=NOW)
        text = matters.render()
        self.assertIn(f"M{mid} Flights to Lisbon — Kim's, not his", text)
        self.assertIn("next: airline confirms the seat (their move)", text)
        self.assertIn("his items: #7", text)


class TestFoldingMailIn(Store):
    def test_a_new_matter_from_the_mail_it_cites(self):
        self.fold({"matter": "new", "email_ids": ["e1"], "title": "Acme interview process",
                   "owner": None, "stage": "passed technical; onsite next", "move": "his",
                   "next_date": "2026-12-03", "date_quote": "on December 3",
                   "people": ["Sam Rivera", "Acme"]})
        (m,) = matters.open_matters()
        self.assertEqual((m["title"], m["move"], m["next_date"], m["owner"]),
                         ("Acme interview process", "his", "2026-12-03", None))
        self.assertEqual(m["people"], "Sam Rivera, Acme")

    def test_a_change_citing_no_email_it_was_shown_or_no_real_matter_is_ignored(self):
        self.fold({"matter": "new", "email_ids": ["zz"], "title": "Invented"},
                  {"matter": 99, "email_ids": ["e1"], "stage": "x"},
                  {"matter": "new", "email_ids": ["e1"]})            # no title
        self.assertEqual(matters.open_matters(), [])

    def test_a_date_whose_words_are_not_in_the_mail_is_dropped(self):
        self.fold({"matter": "new", "email_ids": ["e1"], "title": "Acme interview process",
                   "next_date": "2026-12-09", "date_quote": "on December 9"})
        self.assertIsNone(matters.open_matters()[0]["next_date"])

    def test_whose_move_is_said_once_and_long_text_ends_at_a_word(self):
        self.fold({"matter": "new", "email_ids": ["e1"], "title": "Acme interview process",
                   "next_step": "Sam proposes onsite dates (their move)", "move": "theirs",
                   "latest": "Wed Nov 18: " + "word " * 100})
        m = matters.open_matters()[0]
        self.assertEqual(m["next_step"], "Sam proposes onsite dates")
        self.assertTrue(m["latest"].endswith("word…"))
        self.assertEqual(matters.render().count("their move"), 1)

    def test_a_date_on_his_calendar_counts(self):
        self.fold({"matter": "new", "email_ids": ["e2"], "title": "Acme interview process",
                   "next_date": "2026-12-03", "date_quote": "Acme onsite"},
                  calendar="Thu Dec 3, 10:00 AM — Acme onsite")
        self.assertEqual(matters.open_matters()[0]["next_date"], "2026-12-03")

    def test_later_mail_updates_the_matter_it_continues(self):
        mid = matters.create({"title": "Acme interview process", "stage": "technical round",
                              "move": "theirs"}, now=NOW)
        self.fold({"matter": f"M{mid}", "email_ids": ["e1", "e2"], "stage": "onsite next",
                   "latest": "Wed Nov 18: he said he'll confirm by Friday", "move": "his"})
        m = matters.get(mid)
        self.assertEqual((m["stage"], m["move"]), ("onsite next", "his"))
        self.assertEqual(len(matters.open_matters()), 1)

    def test_his_own_name_or_his_is_no_owner_and_mail_never_clears_one_he_set(self):
        self.fold({"matter": "new", "email_ids": ["e1"], "title": "A", "owner": "Jo Park"},
                  {"matter": "new", "email_ids": ["e1"], "title": "B", "owner": "his"})
        self.assertEqual({m["owner"] for m in matters.open_matters()}, {None})
        mid = matters.create({"title": "Kim's flights", "owner": "Kim"}, source="chat", now=NOW)
        self.fold({"matter": mid, "email_ids": ["e1"], "owner": None, "stage": "checked in"})
        self.assertEqual(matters.get(mid)["owner"], "Kim")

    def test_items_link_to_one_matter_and_only_open_ones(self):
        a = matters.create({"title": "Acme interview process"}, items=[5], now=NOW)
        b = matters.create({"title": "Porto trip"}, now=NOW)
        self.fold({"matter": b, "email_ids": ["e1"], "commitments": [5, "#6", 40]},
                  open_ids={5, 6})
        self.assertEqual(matters.get(b)["items"], [5, 6])
        self.assertEqual(matters.get(a)["items"], [])

    def test_closing_needs_mail_and_news_reopens_a_recent_close(self):
        mid = matters.create({"title": "Return the Northwind headphones"}, now=NOW)
        self.fold({"matter": mid, "email_ids": ["e1"], "closed": True, "why": "refunded"})
        self.assertEqual(matters.open_matters(), [])
        self.fold({"matter": mid, "email_ids": ["e2"], "stage": "refund reversed"})
        self.assertEqual(matters.get(mid)["status"], "open")


class TestEngineFoldsEachTicksMail(Store):
    def test_mail_both_ways_goes_to_the_matters_judge(self):
        mon = engine.EmailDigestMonitor(now_fn=lambda: NOW)
        mon._motion = {"calendar": "(nothing)", "commitments": "(none)"}
        inbound = [{"id": "e1", "sender": "Sam Rivera <sam@acme.example>",
                    "subject": "Acme next steps", "text": "You passed!",
                    "you_remember": "He is interviewing with Acme."}]
        sent = [{"id": "e2", "to": "Sam <sam@acme.example>", "subject": "Re: Acme next steps",
                 "text": "Great, thanks."}]
        reply = json.dumps([{"matter": "new", "email_ids": ["e1"],
                             "title": "Acme interview process", "move": "his"}])
        with patch("instructions.render_for_prompt", return_value=""), \
             patch.object(engine, "ask", return_value=reply) as ask:
            mon._update_matters(inbound, sent)
        self.assertEqual(ask.call_args.args[0], "matters")
        shown = json.loads(ask.call_args.kwargs["emails"])
        self.assertEqual([e["direction"] for e in shown], ["received", "sent"])
        self.assertEqual(shown[0]["you_remember"], "He is interviewing with Acme.")
        self.assertEqual(matters.open_matters()[0]["title"], "Acme interview process")

    def test_no_mail_no_question(self):
        with patch.object(engine, "ask") as ask:
            engine.EmailDigestMonitor(now_fn=lambda: NOW)._update_matters([], [])
        ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
