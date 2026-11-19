"""What the background side picks up from mail on its own: a deadline an email states, a
promise in mail he sent, something he is waiting on. All of it soft, none of it invented.

Run: python3 -m unittest discover tests
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import engine
from engine import EmailDigestMonitor
from skills import commitment_manager as cm

NOW = datetime(2026, 11, 16, 12, 0)     # a Monday


class TestStatedDate(unittest.TestCase):
    TEXT = "Thanks for applying. Please complete it by Friday, November 20. Good luck!"

    def test_a_quoted_date_in_range_is_accepted(self):
        claim = {"date": "2026-11-20", "quote": "complete it by Friday, November 20"}
        self.assertEqual(engine.stated_date(claim, self.TEXT, NOW), "2026-11-20")

    def test_a_quote_that_is_not_in_the_mail_is_refused(self):
        claim = {"date": "2026-11-20", "quote": "reply by Friday"}
        self.assertIsNone(engine.stated_date(claim, self.TEXT, NOW))

    def test_no_quote_past_dates_far_dates_and_junk_are_refused(self):
        for claim in ({"date": "2026-11-20"}, {"date": "2026-11-10", "quote": "complete it by"},
                      {"date": "2027-11-20", "quote": "complete it by"},
                      {"date": "Friday", "quote": "complete it by"}, None, "2026-11-20"):
            self.assertIsNone(engine.stated_date(claim, self.TEXT, NOW), claim)

    def test_quotes_match_through_entities_and_curly_apostrophes(self):
        claim = {"date": "2026-11-20", "quote": "let's hear by Friday"}
        self.assertEqual(engine.stated_date(claim, "Let&#39;s  hear by Friday", NOW), "2026-11-20")
        self.assertEqual(engine.stated_date(claim, "Let’s hear by Friday", NOW), "2026-11-20")


class TestOwnWords(unittest.TestCase):
    def test_the_quoted_thread_is_dropped(self):
        text = ("Sounds good, I'll send the deck Monday.\n\n"
                "On Mon, Nov 16, 2026 at 3:00 PM Sam Rivera <sam@northwind.example> wrote:\n"
                "> I'll send you the contract tomorrow.\n")
        self.assertEqual(engine.own_words(text), "Sounds good, I'll send the deck Monday.")

    def test_a_wrapped_header_and_inline_quotes(self):
        text = ("Yes to both.\n> Can you do Tuesday?\nTuesday works.\n"
                "On Mon, Nov 16, 2026 at 3:00 PM Sam Rivera <sam@northwind.example>\nwrote:\nold text")
        self.assertEqual(engine.own_words(text), "Yes to both.\nTuesday works.")

    def test_a_sentence_starting_with_on_is_kept(self):
        self.assertIn("On Friday I'll call", engine.own_words("On Friday I'll call the bank."))


class TestAlreadyTracked(unittest.TestCase):
    OPEN = [{"id": 5, "description": "Send Kim Lee the lease documents"},
            {"id": 6, "description": "Waiting on Sam Rivera: answer on the start date"}]

    def test_same_matter_is_found(self):
        self.assertEqual(engine.already_tracked("Send the lease documents", self.OPEN)["id"], 5)

    def test_a_shared_name_alone_is_not_the_same_matter(self):
        self.assertIsNone(engine.already_tracked("Send Sam Rivera the deck", self.OPEN, who="Sam Rivera"))


class TestLapse(unittest.TestCase):
    def item(self, **kw):
        base = {"id": 1, "kind": "promise", "source": "sent", "due_date": None,
                "created_at": "2026-11-01 09:00:00", "description": "Send Kim the deck"}
        return dict(base, **kw)

    def test_soft_items_lapse_on_their_own_allowance(self):
        self.assertTrue(engine._has_lapsed(self.item(), NOW))                       # 15 days, 14 allowed
        self.assertFalse(engine._has_lapsed(self.item(created_at="2026-11-05 09:00:00"), NOW))
        self.assertTrue(engine._has_lapsed(self.item(kind="reply_owed", source="email",
                                                     created_at="2026-11-08 09:00:00"), NOW))
        self.assertTrue(engine._has_lapsed(self.item(kind="waiting_on"), NOW))

    def test_a_dated_item_is_counted_from_its_date(self):
        dated = self.item(kind="deadline", source="email", due_date="2026-11-12")
        self.assertFalse(engine._has_lapsed(dated, NOW))                            # 4 days past
        self.assertTrue(engine._has_lapsed(dict(dated, due_date="2026-11-05"), NOW))

    def test_what_he_asked_for_never_lapses(self):
        for kind in ("promise", "deadline", "reply_owed", "waiting_on"):
            self.assertFalse(engine._has_lapsed(self.item(kind=kind, source="chat"), NOW))
        self.assertFalse(engine._has_lapsed(self.item(kind="people_date"), NOW))


class StoreMixin:
    """A throwaway commitments store per test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._p = patch.object(cm, "DB_PATH", os.path.join(self.dir, "c.db"))
        self._p.start()
        cm.init_db()
        self._d = patch("decisions.note")
        self._d.start()

    def tearDown(self):
        self._d.stop()
        self._p.stop()


class TestTrackFromSent(StoreMixin, unittest.TestCase):
    SENT = [{"id": "s1", "to": "Kim Lee <kim@example.com>", "subject": "Re: Porto itinerary",
             "snippet": "", "text": "I'll send you the full itinerary by Thursday. Also, "
                                    "could you confirm the hotel dates?"}]

    def run_judge(self, verdict, sent=None):
        mon, state = EmailDigestMonitor(now_fn=lambda: NOW), {}
        with patch.object(engine, "ask", return_value=json.dumps(verdict)) as ask:
            mon._track_from_sent(state, self.SENT if sent is None else sent)
        return state, ask

    def test_a_promise_and_a_wait_are_tracked_soft_and_announced(self):
        state, ask = self.run_judge([
            {"email_id": "s1", "type": "promise", "what": "Send Kim the full itinerary",
             "who": "Kim Lee", "due": "2026-11-19", "quote": "send you the full itinerary by Thursday"},
            {"email_id": "s1", "type": "waiting", "what": "confirmation of the hotel dates",
             "who": "Kim Lee", "due": None, "quote": "could you confirm the hotel dates?"}])
        self.assertEqual(ask.call_args.args[0], "sent-mail")
        promise, wait = cm.get_open_commitments()
        self.assertEqual((promise["kind"], promise["source"], promise["due_date"]),
                         ("promise", "sent", "2026-11-19"))
        self.assertEqual((wait["kind"], wait["source"], wait["who"]), ("waiting_on", "sent", "Kim Lee"))
        self.assertTrue(wait["description"].startswith("Waiting on Kim Lee:"))
        notes = state["pending_digest"]
        self.assertEqual([n["sender"] for n in notes], ["You → Kim Lee"] * 2)
        self.assertIn(f"tracking your promise as #{promise['id']}, due Thu Nov 19", notes[0]["tag"])
        self.assertIn(f"watching for their answer as #{wait['id']}", notes[1]["tag"])

    def test_words_he_did_not_write_track_nothing(self):
        state, _ = self.run_judge([{"email_id": "s1", "type": "promise", "what": "Send the contract",
                                    "who": "Kim Lee", "due": None, "quote": "I'll send you the contract"}])
        self.assertEqual(cm.get_open_commitments(), [])
        self.assertNotIn("pending_digest", state)

    def test_a_date_his_words_do_not_state_is_dropped_but_the_promise_kept(self):
        self.run_judge([{"email_id": "s1", "type": "promise", "what": "Send Kim the full itinerary",
                         "who": "Kim Lee", "due": "2027-12-25", "quote": "send you the full itinerary"}])
        self.assertIsNone(cm.get_open_commitments()[0]["due_date"])

    def test_something_he_already_tracks_is_not_tracked_twice(self):
        cm.add("Send Kim the Porto itinerary", kind="promise", who="Kim")
        self.run_judge([{"email_id": "s1", "type": "promise", "what": "Send Kim the full itinerary for Porto",
                         "who": "Kim Lee", "due": None, "quote": "send you the full itinerary"}])
        self.assertEqual(len(cm.get_open_commitments()), 1)

    def test_no_text_means_no_model_call_and_junk_verdicts_are_ignored(self):
        _, ask = self.run_judge([], sent=[{"id": "s2", "to": "x", "subject": "y", "snippet": "", "text": ""}])
        ask.assert_not_called()
        self.run_judge([{"email_id": "nope", "type": "promise", "what": "x", "quote": "send you"},
                        {"email_id": "s1", "type": "gift", "what": "x", "quote": "send you"}, "text"])
        self.assertEqual(cm.get_open_commitments(), [])

    def test_a_new_ask_of_the_same_person_restates_the_old_wait(self):
        """He asked to move the interview earlier; it happened; then he asked whether a
        date after the 30th works. The old wait used to block the new one as a duplicate."""
        old = cm.add("Waiting on Kim Lee: answer on whether the call can move earlier",
                     kind="waiting_on", who="Kim Lee", source="sent")
        state, _ = self.run_judge([{"email_id": "s1", "type": "waiting",
                                    "what": "confirmation of the hotel dates", "who": "Kim Lee",
                                    "due": None, "quote": "could you confirm the hotel dates?"}])
        (wait,) = cm.get_open_commitments()
        self.assertEqual(wait["id"], old)
        self.assertEqual(wait["description"], "Waiting on Kim Lee: confirmation of the hotel dates")
        self.assertEqual(cm.events_for(old)[-1]["event"], "restated")
        self.assertIn(f"watching for their answer as #{old}", state["pending_digest"][0]["tag"])

    def test_a_wait_on_someone_else_with_the_same_first_name_is_left_alone(self):
        cm.add("Waiting on Kim Park: the signed lease", kind="waiting_on", who="Kim Park")
        self.run_judge([{"email_id": "s1", "type": "waiting", "what": "confirmation of the hotel dates",
                         "who": "Kim Lee", "due": None, "quote": "could you confirm the hotel dates?"}])
        self.assertEqual(sorted(c["who"] for c in cm.get_open_commitments()), ["Kim Lee", "Kim Park"])

    def test_at_most_two_items_from_one_email(self):
        self.run_judge([{"email_id": "s1", "type": "promise", "what": w, "who": "Kim Lee",
                         "due": None, "quote": "send you the full itinerary"}
                        for w in ("Alpha bravo task", "Charlie delta task", "Echo foxtrot task")])
        self.assertEqual(len(cm.get_open_commitments()), 2)


class TestWaitingOn(StoreMixin, unittest.TestCase):
    def reconcile(self, verdict, mail):
        import situation
        cid = cm.add("Waiting on Sam Rivera: answer on the start date", kind="waiting_on",
                     who="Sam Rivera", source="sent")
        created = datetime.fromisoformat(cm.get_open_commitments()[0]["created_at"]).timestamp()
        for m in mail:
            m["ts"] = created + m.pop("after")
        sit = {"calendar": [], "mail": {cid: mail}}
        verdict = [dict(v, commitment_id=cid) for v in verdict]
        with patch.object(situation, "around", return_value=sit), \
             patch.object(engine, "ask", return_value=json.dumps(verdict)):
            return engine.reconcile(datetime.now()), cid

    def test_their_reply_closes_it_and_tells_him(self):
        notes, _ = self.reconcile(
            [{"source": "mail", "quote": "Re: Offer details", "evidence": "the start date is flexible"}],
            [{"direction": "received", "who": "Sam Rivera <sam@northwind.example>",
              "subject": "Re: Offer details", "snippet": "Any Monday in January.", "after": 3600}])
        self.assertEqual(cm.get_open_commitments(), [])
        self.assertIn("got back to you", notes[0].text)
        self.assertIn("Sam Rivera", notes[0].text)

    def test_his_own_follow_up_or_older_mail_closes_nothing(self):
        for mail in ([{"direction": "sent", "who": "Sam Rivera <sam@northwind.example>",
                       "subject": "Re: Offer details", "snippet": "checking in", "after": 3600}],
                     [{"direction": "received", "who": "Sam Rivera <sam@northwind.example>",
                       "subject": "Re: Offer details", "snippet": "earlier note", "after": -3600}]):
            notes, cid = self.reconcile(
                [{"source": "mail", "quote": "Re: Offer details", "evidence": "x"}], mail)
            self.assertEqual([c["id"] for c in cm.get_open_commitments() if c["id"] == cid], [cid])
            self.assertEqual(notes, [])
            cm.drop(cid)

    def test_listed_apart_from_his_own_items(self):
        cm.add("Renew passport", kind="deadline", due_date="2026-12-01")
        cm.add("Waiting on Sam Rivera: signed contract", kind="waiting_on", who="Sam Rivera")
        with patch("situation.describe", return_value=""):
            out = cm.list_commitments.invoke({})
        own, theirs = out.split("Waiting on others")
        self.assertIn("Renew passport", own)
        self.assertNotIn("signed contract", own)
        self.assertIn("signed contract", theirs)


class TestNewestWordReachesTheDigest(StoreMixin, unittest.TestCase):
    def test_an_alert_also_goes_to_the_digest_marked_as_already_sent(self):
        mon = EmailDigestMonitor(now_fn=lambda: NOW)
        state = {"alerts": [{"sender": "Sam Rivera", "subject": "Acme next steps",
                             "reason": "You're through to the onsite."}]}
        with patch.object(mon, "_screen_new_mail", return_value=[]), \
             patch.object(mon, "_close_proven", return_value=[]), \
             patch.object(mon, "_track_from_sent"), \
             patch.object(mon, "_maybe_flush_digest", return_value=[]):
            out = mon.check(state)
        self.assertIn("through to the onsite", out[0].text)
        self.assertEqual(state["pending_digest"][0]["alerted"], True)

    def test_the_digest_knows_the_time_and_skips_if_all_was_already_sent(self):
        mon = EmailDigestMonitor(now_fn=lambda: NOW.replace(hour=19))
        state = {"pending_digest": [{"sender": "Sam", "subject": "s", "reason": "r", "alerted": True}]}
        self.assertEqual(mon._maybe_flush_digest(dict(state)), [])
        state["pending_digest"].append({"sender": "Kim", "subject": "Dinner", "reason": "asks about Sat",
                                        "at": "Tue 7:31 PM"})
        with patch.object(EmailDigestMonitor, "_as_of_now", side_effect=lambda items: items), \
             patch("situation.in_motion", return_value={"calendar": "", "commitments": "",
                                                         "matters": ""}), \
             patch("instructions.render_for_prompt", return_value=""), \
             patch.object(engine, "ask", return_value='{"bullets": ["Kim: dinner Saturday"]}') as ask:
            (digest,) = mon._maybe_flush_digest(state)
        self.assertIn("Kim: dinner Saturday", digest.text)
        self.assertIn(f"{NOW.day}, 7:", ask.call_args.kwargs["now"])
        self.assertIn('"alerted": true', ask.call_args.kwargs["notes"])


class TestMorningMessages(StoreMixin, unittest.TestCase):
    def test_a_tracking_tag_for_something_since_closed_is_corrected(self):
        open_id = cm.add("Reply to Kim", kind="reply_owed")
        done_id = cm.add("Reply to Sam", kind="reply_owed")
        cm.complete(done_id)
        out = engine._as_of_morning([f"📩 Sam — next steps (tracking reply as #{done_id})",
                                     f"📩 Kim — dinner (tracking reply as #{open_id})"])
        self.assertEqual(out[0], "📩 Sam — next steps (handled since)")
        self.assertIn(f"#{open_id}", out[1])


class TestDigestTags(unittest.TestCase):
    def test_tag_for_each_kind_of_tracked_note(self):
        self.assertEqual(engine.tracking_tag({"tracked": "#9"}), "tracking reply as #9")
        self.assertEqual(engine.tracking_tag({"tracked": "#9", "tag": "tracking as #9, due Fri Nov 20"}),
                         "tracking as #9, due Fri Nov 20")
        self.assertEqual(engine.tracking_tag({"tracked": "(you already owe a reply on this thread)"}),
                         "you already owe a reply on this thread")


if __name__ == "__main__":
    unittest.main()
