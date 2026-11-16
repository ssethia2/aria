"""The decision record: what each background judge was shown, said, and what was done.

Run: python3 -m unittest discover tests
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import decisions
import engine


class RecordMixin:
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.unlink(self.path)
        self._p = patch.object(decisions, "LOG_PATH", self.path)
        self._p.start()
        decisions._pruned_on = None

    def tearDown(self):
        self._p.stop()
        for p in (self.path, self.path + ".tmp"):
            if os.path.exists(p):
                os.unlink(p)


class TestRecord(RecordMixin, unittest.TestCase):
    def test_a_question_and_its_answer_are_kept(self):
        decisions.record("email-screen", {"emails": [{"id": "m1"}], "rules": "(none)"}, "[]")
        (entry,) = decisions.read()
        self.assertEqual(entry["judge"], "email-screen")
        self.assertEqual(entry["reply"], "[]")
        self.assertEqual(json.loads(entry["fields"]["emails"]), [{"id": "m1"}])
        self.assertEqual(entry["fields"]["rules"], "(none)")

    def test_notes_sit_beside_the_question_in_order(self):
        decisions.record("reconcile", {"commitments": "#7 Reply to Kim"}, "[]")
        decisions.note("reconcile", "rejected close of #7")
        kinds = ["note" if "note" in e else "question" for e in decisions.read(judge="reconcile")]
        self.assertEqual(kinds, ["question", "note"])
        self.assertEqual(len(decisions.read(judge="reconcile", questions_only=True)), 1)

    def test_read_filters_by_judge_subject_and_count(self):
        for i in range(5):
            decisions.record("chase", {"commitments": f"item {i} about Acme"}, "{}")
        decisions.record("digest", {"notes": "Northwind"}, "{}")
        self.assertEqual(len(decisions.read(judge="chase")), 5)
        self.assertEqual(len(decisions.read(about="northwind")), 1)
        self.assertEqual(len(decisions.read(judge="chase", limit=2)), 2)
        self.assertIn("item 4", decisions.read(judge="chase", limit=1)[0]["fields"]["commitments"])

    def test_old_entries_are_pruned_once_a_day(self):
        now = datetime(2026, 6, 20, 12, 0)
        decisions.record("chase", {"x": "old"}, "{}", now=now - timedelta(days=decisions.KEEP_DAYS + 2))
        decisions.record("chase", {"x": "recent"}, "{}", now=now - timedelta(days=1))
        decisions._pruned_on = None
        decisions.record("chase", {"x": "today"}, "{}", now=now)
        self.assertEqual([e["fields"]["x"] for e in decisions.read()], ["recent", "today"])

    def test_a_huge_field_is_capped(self):
        decisions.record("chase", {"commitments": "x" * (decisions.FIELD_LIMIT * 2)}, "{}")
        self.assertEqual(len(decisions.read()[0]["fields"]["commitments"]), decisions.FIELD_LIMIT)

    def test_recording_never_raises(self):
        with patch.object(decisions, "_append", side_effect=OSError("disk full")):
            decisions.record("chase", {"x": "y"}, "{}")
            decisions.note("chase", "did a thing")

    def test_a_torn_line_is_skipped(self):
        decisions.record("chase", {"x": "y"}, "{}")
        with open(self.path, "a") as f:
            f.write('{"at": "2026-06-20T12:00:00", "judge": "cha\n')
        self.assertEqual(len(decisions.read()), 1)


class TestExplain(RecordMixin, unittest.TestCase):
    def test_shows_what_was_seen_said_and_done(self):
        decisions.record("reconcile", {
            "commitments": "#7 [reply_owed] Reply to Kim Lee: Dinner on Saturday?",
            "situation": "Mon, HE SENT to Kim Lee: “Re: Dinner on Saturday?” — works for me\n"
                         "Tue, received from Acme: “Your invoice”"},
            '[{"commitment_id": 7, "source": "mail", "quote": "Re: Dinner on Saturday?"}]')
        decisions.note("reconcile", "closed #7 ('Reply to Kim Lee: Dinner on Saturday?') on your email")
        out = decisions.explain("Kim")
        self.assertIn("reconcile", out)
        self.assertIn("did: closed #7", out)
        self.assertIn("saw situation: Mon, HE SENT to Kim Lee", out)
        self.assertNotIn("invoice", out)             # only the lines about the subject
        self.assertIn('said: [{"commitment_id": 7', out)
        self.assertLess(out.index("did: closed #7"), out.index("said:"))   # newest first

    def test_an_answer_that_leaves_the_subject_out_says_so(self):
        decisions.record("email-screen", {"emails": "from Northwind: an update " + "x" * 50},
                         json.dumps([{"id": f"m{i}", "reason": "something else " * 8}
                                     for i in range(8)]))
        self.assertIn("chose not to act on it", decisions.explain("northwind"))

    def test_nothing_recorded_tells_her_not_to_guess(self):
        out = decisions.explain("Porto")
        self.assertIn("Nothing in the last", out)
        self.assertIn("rather than guessing", out)

    def test_needs_a_subject(self):
        self.assertIn("Say what it's about", decisions.explain("  "))


class TestAsk(RecordMixin, unittest.TestCase):
    """Every background judge asks through engine.ask."""

    def llm(self, content):
        self.prompts = []
        return SimpleNamespace(invoke=lambda prompt: (self.prompts.append(prompt),
                                                      SimpleNamespace(content=content))[1])

    def test_formats_the_registered_prompt_and_records_the_decision(self):
        text = engine.ask("email-proof", llm=self.llm("[]"),
                          commitments="#4 Finish the assessment", emails="[]")
        self.assertEqual(text, "[]")
        self.assertIn("#4 Finish the assessment", self.prompts[0])
        self.assertIn("PROVE", self.prompts[0])
        (entry,) = decisions.read()
        self.assertEqual((entry["judge"], entry["reply"]), ("email-proof", "[]"))
        self.assertEqual(entry["fields"]["commitments"], "#4 Finish the assessment")

    def test_thinking_blocks_are_dropped_from_the_answer(self):
        blocks = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": '{"send": false}'}]
        text = engine.ask("chase", llm=self.llm(blocks), today="2026-06-10", commitments="[]",
                          rules="(none)", told="(nothing)", situation="(nothing)")
        self.assertEqual(text, '{"send": false}')

    def test_record_false_leaves_no_trace(self):
        engine.ask("email-proof", llm=self.llm("[]"), record=False, commitments="x", emails="[]")
        self.assertEqual(decisions.read(), [])

    def test_every_judge_prompt_formats_with_its_own_fields(self):
        from evals import harness as H
        from datetime import date
        for judge in engine.JUDGES:
            fields = H.judge_fields(judge, {}, date(2026, 6, 10))
            engine.ask(judge, llm=self.llm("{}"), record=False, **fields)
            self.assertNotIn("{" + next(iter(fields)) + "}", self.prompts[0])

    def test_tiers(self):
        self.assertEqual(engine.JUDGES["reconcile"][1], "standard")
        self.assertEqual(engine.JUDGES["email-screen"][1], "light")


class TestCheckVerdict(unittest.TestCase):
    def setUp(self):
        from evals import harness
        self.check = harness.check_verdict

    def test_items_where_has_absent_optional(self):
        verdict = [{"id": "m1", "needs_reply": False, "reason": "confirms it"}]
        self.assertEqual(self.check({"items": [{"where": {"id": "m1"}, "has": {"needs_reply": False}}]},
                                    verdict, ""), [])
        self.assertTrue(self.check({"items": [{"where": {"id": "m1"}, "has": {"needs_reply": True}}]},
                                   verdict, ""))
        self.assertTrue(self.check({"items": [{"where": {"id": "m2"}}]}, verdict, ""))
        self.assertEqual(self.check({"items": [{"where": {"id": "m2"}, "optional": True,
                                                "has": {"needs_reply": False}}]}, verdict, ""), [])
        self.assertEqual(self.check({"items": [{"where": {"id": "m2"}, "absent": True}]}, verdict, ""), [])
        self.assertTrue(self.check({"items": [{"where": {"id": "m1"}, "absent": True}]}, verdict, ""))

    def test_ids_match_with_or_without_a_hash(self):
        self.assertEqual(self.check({"items": [{"where": {"commitment_id": 7}}]},
                                    [{"commitment_id": "#7"}], ""), [])

    def test_count_of_a_list_or_a_named_list(self):
        self.assertEqual(self.check({"count": 0}, [], ""), [])
        self.assertTrue(self.check({"count": 0}, [{"id": "m1"}], ""))
        self.assertEqual(self.check({"count": {"of": "bullets", "min": 1, "max": 2}},
                                    {"bullets": ["a", "b"]}, ""), [])
        self.assertTrue(self.check({"count": {"of": "bullets", "max": 1}}, {"bullets": ["a", "b"]}, ""))
        self.assertTrue(self.check({"count": 1}, {"send": False}, ""))

    def test_verdict_fields_and_text(self):
        v = {"send": False, "message": ""}
        self.assertEqual(self.check({"verdict": {"send": False}}, v, ""), [])
        self.assertTrue(self.check({"verdict": {"send": True}}, v, ""))
        self.assertEqual(self.check({"text_matches": ["visa"], "text_not_matches": ["party"]},
                                    {}, '{"message": "Your VISA form"}'), [])
        self.assertTrue(self.check({"text_not_matches": ["party"]}, {}, "the party"))


class TestRunJudgeScenario(RecordMixin, unittest.TestCase):
    def test_runs_through_ask_without_recording(self):
        from datetime import date
        from evals import harness as H
        sc = {"id": "x", "judge": "email-proof", "why": "w",
              "fields": {"commitments": "#4 Finish it", "emails": [{"id": "s1"}]},
              "expect": {"count": 1, "items": [{"where": {"commitment_id": 4}}]}}
        with patch.object(engine, "ask", return_value='[{"commitment_id": 4, "email_id": "s1"}]') as ask:
            r = H.run_judge_scenario(sc, date(2026, 6, 10))
        self.assertTrue(r["passed"], r["turns"][0]["failures"])
        self.assertIs(ask.call_args.kwargs["record"], False)
        self.assertEqual(json.loads(ask.call_args.kwargs["emails"]), [{"id": "s1"}])
        self.assertEqual(decisions.read(), [])

    def test_a_wrong_verdict_fails_with_the_reason(self):
        from datetime import date
        from evals import harness as H
        sc = {"id": "x", "judge": "email-proof", "why": "w", "fields": {}, "expect": {"count": 0}}
        with patch.object(engine, "ask", return_value='[{"commitment_id": 4}]'):
            r = H.run_judge_scenario(sc, date(2026, 6, 10))
        self.assertFalse(r["passed"])
        self.assertIn("expected 0 item(s), got 1", r["turns"][0]["failures"])

    def test_replay_reasks_recorded_questions_and_counts_changes(self):
        import contextlib
        import io
        from evals import harness as H
        decisions.record("email-proof", {"commitments": "#4 Finish it", "emails": "[]"}, "[]")
        decisions.record("email-proof", {"commitments": "#5 Send it", "emails": "[]"}, "[]")
        decisions.record("email-proof", {"old_field": "x"}, "[]")      # an older prompt's shape
        answers = iter(["[]", '[{"commitment_id": 5}]'])
        out = io.StringIO()
        with patch.object(engine, "ask", side_effect=lambda *a, **k: next(answers)) as ask, \
             contextlib.redirect_stdout(out):
            self.assertEqual(H.replay("email-proof", last=10), 0)
        self.assertEqual(ask.call_count, 2)
        self.assertTrue(all(c.kwargs["record"] is False for c in ask.call_args_list))
        self.assertIn("1 of 2 replayed", out.getvalue())
        self.assertIn("1 skipped", out.getvalue())
        self.assertEqual(len(decisions.read()), 3)                      # replay records nothing


if __name__ == "__main__":
    unittest.main()
