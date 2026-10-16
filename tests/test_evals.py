"""The eval harness itself — offline (no LLM, no network). Keeps the suite trustworthy:
scenarios can't rot against renamed tools, templates resolve, assertions grade correctly,
and the sandbox can never let a faked tool reach a real account or leak state.
Run: python3 -m unittest discover tests
"""
import os
import re
import unittest
from datetime import date
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from evals import harness as H
from evals.sandbox import Sandbox, REAL_TOOLS, DEFAULT_FAKES, GENERIC_FAKE

TODAY = date(2026, 10, 15)    # a Thursday


def _owner_tool_names():
    import agent_core
    return {t.name for t in agent_core.build_tools(guest=False)}


class TestScenarioFiles(unittest.TestCase):
    """Static validity of every scenario — catches rot when tools are renamed/removed."""

    @classmethod
    def setUpClass(cls):
        cls.scenarios = H.load_scenarios()

    def test_there_are_scenarios_and_ids_are_unique(self):
        ids = [s["id"] for s in self.scenarios]
        self.assertGreaterEqual(len(ids), 15)
        self.assertEqual(len(ids), len(set(ids)), "duplicate scenario ids")

    def test_every_scenario_is_well_formed(self):
        allowed_expect = {"called", "not_called", "called_any", "called_with",
                          "reply_matches", "reply_not_matches", "mentions_date", "path",
                          "db", "rules_count", "judge"}
        for s in self.scenarios:
            self.assertTrue(s.get("why"), f"{s['id']}: say WHY the scenario exists")
            self.assertTrue(s.get("turns"), f"{s['id']}: no turns")
            for t in s["turns"]:
                self.assertTrue(t.get("user") or t.get("photo"), f"{s['id']}: empty turn")
                unknown = set(t.get("expect") or {}) - allowed_expect
                self.assertFalse(unknown, f"{s['id']}: unknown expectation keys {unknown}")

    def test_every_referenced_tool_exists(self):
        tools = _owner_tool_names()
        for s in self.scenarios:
            refs = set((s.get("tools") or {}).keys())
            for t in s["turns"]:
                e = t.get("expect") or {}
                refs |= set(e.get("called", []) or []) | set(e.get("not_called", []) or [])
                refs |= set(e.get("called_any", []) or []) | set((e.get("called_with") or {}))
            missing = refs - tools
            self.assertFalse(missing, f"{s['id']} references unknown tools {missing}")

    def test_called_with_args_exist_on_the_tool(self):
        import agent_core
        schemas = {t.name: set(t.args) for t in agent_core.build_tools(guest=False)}
        for s in self.scenarios:
            for t in s["turns"]:
                for tool_name, args in ((t.get("expect") or {}).get("called_with") or {}).items():
                    extra = set(args) - schemas[tool_name]
                    self.assertFalse(extra, f"{s['id']}: {tool_name} has no args {extra}")

    def test_all_templates_resolve_and_regexes_compile(self):
        for s in self.scenarios:
            r = H.resolve(s, TODAY)
            self.assertNotIn("{{", repr(r), f"{s['id']}: unresolved template")
            for t in r["turns"]:
                e = t.get("expect") or {}
                for pat in (e.get("reply_matches") or []) + (e.get("reply_not_matches") or []):
                    re.compile(pat)

    def test_fakes_only_cover_real_tools_and_allowlist_is_real(self):
        tools = _owner_tool_names()
        self.assertFalse(set(DEFAULT_FAKES) - tools, "fake for a tool that doesn't exist")
        self.assertFalse(REAL_TOOLS - tools, "allowlisted tool that doesn't exist")
        self.assertFalse(REAL_TOOLS & set(DEFAULT_FAKES), "tool both real and faked")


class TestTemplates(unittest.TestCase):
    def test_relative_dates_and_formats(self):
        self.assertEqual(H.resolve("{{today}}", TODAY), "2026-10-15")
        self.assertEqual(H.resolve("{{today+3}}", TODAY), "2026-10-18")
        self.assertEqual(H.resolve("{{today-3}}", TODAY), "2026-10-12")
        self.assertEqual(H.resolve("{{weekday:today+3}}", TODAY), "Sunday")
        self.assertEqual(H.resolve("{{wd:today+1}}", TODAY), "Fri")
        self.assertEqual(H.resolve("{{longdate:today+7}}", TODAY), "Thursday, October 22")
        self.assertEqual(H.resolve("{{monthday:today+2}}", TODAY), "October 17")

    def test_annual_is_next_occurrence(self):
        self.assertEqual(H.resolve("{{annual:03-14}}", TODAY), "2027-03-14")  # passed
        self.assertEqual(H.resolve("{{annual:12-25}}", TODAY), "2026-12-25")  # upcoming
        self.assertEqual(H.resolve("{{annual:10-15}}", TODAY), "2026-10-15")  # today

    def test_nested_structures(self):
        r = H.resolve({"a": ["{{today+1}}", {"b": "x {{weekday:today}} y"}]}, TODAY)
        self.assertEqual(r, {"a": ["2026-10-16", {"b": "x Thursday y"}]})

    def test_date_mentions(self):
        hit = lambda reply, iso: any(re.search(p, reply, re.I)  # noqa: E731
                                     for p in H.date_mentions(iso, TODAY))
        self.assertTrue(hit("Rent is due Saturday.", "2026-10-17"))
        self.assertTrue(hit("Due Oct 17th.", "2026-10-17"))
        self.assertTrue(hit("It's due tomorrow", "2026-10-16"))
        self.assertTrue(hit("due 10/17", "2026-10-17"))
        self.assertFalse(hit("Rent is due Friday.", "2026-10-17"))
        self.assertFalse(hit("due October 18", "2026-10-17"))


class TestMatchers(unittest.TestCase):
    def test_norm_time(self):
        for raw in ("19:30", "7:30pm", "7:30 PM", "19:30:00", "7:30 p.m."):
            self.assertEqual(H.norm_time(raw), "19:30", raw)
        self.assertEqual(H.norm_time("5pm"), "17:00")
        self.assertEqual(H.norm_time("12am"), "00:00")
        self.assertEqual(H.norm_time("noon"), "noon")
        self.assertIsNone(H.norm_time(None))

    def test_match_value(self):
        self.assertTrue(H.match_value("2026-10-18", "2026-10-18"))
        self.assertTrue(H.match_value("Shared", "shared"))
        self.assertTrue(H.match_value(None, None))
        self.assertTrue(H.match_value("", None))
        self.assertFalse(H.match_value("2026-10-19", None))
        self.assertTrue(H.match_value("personal_only", {"contains": "personal"}))
        self.assertTrue(H.match_value("b", {"one_of": ["a", "b"]}))
        self.assertTrue(H.match_value("19:30", {"time": "7:30pm"}))
        self.assertFalse(H.match_value("19:00", {"time": "7:30pm"}))


def _call(tool_name, **args):
    return {"tool": tool_name, "args": args, "turn": 0}


class TestCheckTurn(unittest.TestCase):
    def check(self, expect, reply="", path="agent", calls=(), db=(), rules=()):
        return H.check_turn(expect, reply, path, list(calls), list(db), list(rules), TODAY)

    def test_called_and_not_called(self):
        calls = [_call("list_commitments"), _call("complete_commitment", commitment_id=2)]
        self.assertEqual(self.check({"called": ["list_commitments"]}, calls=calls), [])
        self.assertTrue(self.check({"called": ["reopen_commitment"]}, calls=calls))
        self.assertTrue(self.check({"not_called": ["complete_commitment"]}, calls=calls))
        self.assertEqual(self.check({"called_any": ["x", "list_commitments"]}, calls=calls), [])

    def test_called_with_any_matching_call(self):
        calls = [_call("create_calendar_event", date_iso="2026-10-17", start_time="09:00"),
                 _call("create_calendar_event", date_iso="2026-10-17", start_time="19:30")]
        want = {"create_calendar_event": {"date_iso": "2026-10-17",
                                          "start_time": {"time": "7:30pm"}}}
        self.assertEqual(self.check({"called_with": want}, calls=calls), [])
        bad = {"create_calendar_event": {"date_iso": "2026-10-18"}}
        self.assertTrue(self.check({"called_with": bad}, calls=calls))
        self.assertTrue(self.check({"called_with": {"get_weather": {"days": 1}}}))

    def test_reply_patterns_path_and_dates(self):
        self.assertEqual(self.check({"reply_matches": ["girls night"]},
                                    reply="Girls Night on Sun"), [])
        self.assertTrue(self.check({"reply_not_matches": ["your description"]},
                                   reply="Based on your description..."))
        self.assertTrue(self.check({"path": "quick"}, path="agent"))
        self.assertEqual(self.check({"mentions_date": "2026-10-17"}, reply="due Saturday"), [])
        self.assertTrue(self.check({"mentions_date": "2026-10-17"}, reply="due Friday"))

    def test_db_expectations(self):
        db = [{"id": 1, "description": "Dentist appointment", "status": "open",
               "due_date": "2026-10-19", "due_time": None, "who": None, "kind": "deadline",
               "snoozed_until": None},
              {"id": 2, "description": "Write card", "status": "done",
               "due_date": "2026-10-17", "due_time": "17:00", "who": "Mom",
               "kind": "promise", "snoozed_until": None}]
        ok = {"db": {"open_count": 1, "done_count": 1, "items": [
            {"match": "dentist", "status": "open", "due_date": "2026-10-19"},
            {"match": "card", "due_time": "5pm", "who": "mom"},
            {"match": "passport", "optional": True},
            {"match": "gym", "absent": True}]}}
        self.assertEqual(self.check(ok, db=db), [])
        self.assertTrue(self.check({"db": {"open_count": 2}}, db=db))
        self.assertTrue(self.check({"db": {"items": [{"match": "dentist",
                                                      "due_date": "2026-10-20"}]}}, db=db))
        self.assertTrue(self.check({"db": {"items": [{"match": "passport"}]}}, db=db))
        self.assertTrue(self.check({"db": {"items": [{"match": "dentist",
                                                      "absent": True}]}}, db=db))
        self.assertTrue(self.check({"db": {"items": [{"match": "dentist",
                                                      "due_date": None}]}}, db=db))

    def test_rules_count(self):
        self.assertEqual(self.check({"rules_count": 3}, rules=["a", "b", "c"]), [])
        self.assertTrue(self.check({"rules_count": 3}, rules=["a", "b", "c", "d"]))


class TestSandbox(unittest.TestCase):
    def test_schemas_identical_to_production(self):
        import agent_core
        real = {t.name: t for t in agent_core.build_tools(guest=False)}
        with Sandbox() as sb:
            for t in sb.tools:
                self.assertEqual(t.description, real[t.name].description, t.name)
                self.assertEqual(t.args, real[t.name].args, t.name)

    def test_real_tools_hit_throwaway_stores_only(self):
        from skills import commitment_manager as cm
        import instructions, people, memory
        real_paths = (cm.DB_PATH, instructions.INSTRUCTIONS_PATH, people.PEOPLE_PATH,
                      memory.PROFILE_PATH)
        with Sandbox() as sb:
            for p in (cm.DB_PATH, instructions.INSTRUCTIONS_PATH, people.PEOPLE_PATH,
                      memory.PROFILE_PATH):
                self.assertTrue(p.startswith(sb.dir), p)
            tools = {t.name: t for t in sb.tools}
            out = tools["add_commitment"].invoke({"description": "call mom",
                                                  "due_date_iso": "2026-10-20"})
            self.assertIn("call mom", out)
            self.assertEqual([r["description"] for r in sb.commitments()], ["call mom"])
            self.assertEqual(len(sb.standing_rules()), 3)        # fixture rules loaded
            self.assertEqual(sb.calls[-1]["tool"], "add_commitment")
            self.assertEqual(sb.calls[-1]["args"]["due_date_iso"], "2026-10-20")
            tmp = sb.dir
        self.assertEqual((cm.DB_PATH, instructions.INSTRUCTIONS_PATH, people.PEOPLE_PATH,
                          memory.PROFILE_PATH), real_paths)       # restored on exit
        self.assertFalse(os.path.exists(tmp))                     # temp state removed

    def test_faked_tools_never_reach_their_implementation(self):
        @tool
        def dangerous(x: str) -> str:
            """A tool that would touch a real account."""
            raise AssertionError("real implementation reached!")

        with Sandbox() as sb:
            self.assertEqual(sb._proxy(dangerous).invoke({"x": "1"}), GENERIC_FAKE)
            tools = {t.name: t for t in sb.tools}
            with patch("skills.google_calendar.get_calendar_service",
                       side_effect=AssertionError("calendar reached!")):
                out = tools["create_calendar_event"].invoke(
                    {"title": "Dinner", "date_iso": "2026-10-17", "start_time": "19:30"})
            self.assertIn("Created 'Dinner' on 2026-10-17 19:30", out)

    def test_overrides_sequence_and_arg_rendering(self):
        with Sandbox(overrides={"web_search": ["first {query}", "second"],
                                "list_commitments": "overridden"}) as sb:
            tools = {t.name: t for t in sb.tools}
            ws = tools["web_search"]
            self.assertEqual(ws.invoke({"query": "salute"}), "first salute")
            self.assertEqual(ws.invoke({"query": "x"}), "second")
            self.assertEqual(ws.invoke({"query": "y"}), "second")      # last repeats
            self.assertEqual(tools["list_commitments"].invoke({}), "overridden")

    def test_side_channels_sealed_and_restored(self):
        import agent_core, brain_sdk, notify
        orig = (agent_core.build_tools, notify.send_telegram, brain_sdk.STATE_PATH)
        with Sandbox() as sb:
            self.assertTrue(notify.send_telegram("would ping your phone"))
            self.assertTrue(str(brain_sdk.STATE_PATH).startswith(sb.dir))
            self.assertEqual([t.name for t in agent_core.build_tools()],
                             [t.name for t in sb.tools])
            with self.assertRaises(RuntimeError):
                agent_core.build_tools(guest=True)
        self.assertEqual((agent_core.build_tools, notify.send_telegram,
                          brain_sdk.STATE_PATH), orig)


class TestRegressionHistory(unittest.TestCase):
    def test_each_scenario_compares_to_its_own_latest_result(self):
        import json, tempfile
        d = tempfile.mkdtemp()
        runs = {"20261015-100000.json": {"brain": "subscription", "scenarios": {
                    "a": {"passed": 0, "trials": 1}, "b": {"passed": 1, "trials": 1}}},
                "20261015-110000.json": {"brain": "api", "scenarios": {      # other brain
                    "b": {"passed": 0, "trials": 1}}},
                "20261015-120000.json": {"brain": "subscription", "scenarios": {
                    "a": {"passed": 1, "trials": 1}}}}            # targeted --only run
        for fn, body in runs.items():
            with open(os.path.join(d, fn), "w") as f:
                json.dump(body, f)
        with patch.object(H, "RESULTS_DIR", d):
            prev = H._previous_results("subscription", {"a", "b", "c"})
        self.assertEqual(prev["a"]["passed"], 1)     # newest run that covered a
        self.assertEqual(prev["b"]["passed"], 1)     # older run still counts for b
        self.assertNotIn("c", prev)                  # never run


class _ScriptedAgent:
    """Stands in for the LLM: each invoke runs the next scripted tool calls through the
    sandbox's (proxied) tools and replies — so harness plumbing is tested offline."""

    def __init__(self, script):
        self.script = list(script)

    def invoke(self, payload, config=None):
        import agent_core
        tools = {t.name: t for t in agent_core.build_tools()}
        calls, reply = self.script.pop(0)
        for name, args in calls:
            tools[name].invoke(args)
        return {"messages": [AIMessage(content=reply)]}


class TestRunScenario(unittest.TestCase):
    SC = {"id": "t", "area": "x", "why": "test",
          "setup": {"commitments": [{"description": "Dentist", "due_date": "{{today+1}}"}]},
          "turns": [{"user": "push the dentist to {{weekday:today+4}}",
                     "expect": {"called": ["reschedule_commitment"],
                                "db": {"items": [{"match": "dentist",
                                                  "due_date": "{{today+4}}"}]},
                                "judge": "she confirms"}}]}

    def _run(self, script, judge=(True, "ok")):
        with patch.object(Sandbox, "build_agent", lambda self, brain: _ScriptedAgent(script)), \
             patch("agent_core.quick_answer", return_value=None), \
             patch.object(H, "run_judge", return_value=judge) as j:
            return H.run_scenario(self.SC, "api", 0, TODAY), j

    def test_passing_run(self):
        r, judge = self._run([([("reschedule_commitment",
                                 {"commitment_id": 1, "new_due_date_iso": "2026-10-19"})],
                               "Moved to Monday.")])
        self.assertTrue(r["passed"], r["turns"][0]["failures"])
        self.assertEqual(r["turns"][0]["path"], "agent")
        self.assertEqual(r["turns"][0]["calls"][0]["tool"], "reschedule_commitment")
        judge.assert_called_once()

    def test_wrong_behavior_fails_with_reasons(self):
        r, _ = self._run([([("drop_commitment", {"commitment_id": 1}),
                            ("add_commitment", {"description": "Dentist",
                                                "due_date_iso": "2026-10-19"})],
                           "Done!")], judge=(False, "too chipper"))
        self.assertFalse(r["passed"])
        fails = " | ".join(r["turns"][0]["failures"])
        self.assertIn("reschedule_commitment", fails)
        self.assertIn("judge: too chipper", fails)

    def test_agent_exception_is_a_failure_not_a_crash(self):
        class Boom:
            def invoke(self, *a, **k):
                raise RuntimeError("model exploded")
        with patch.object(Sandbox, "build_agent", lambda self, brain: Boom()), \
             patch("agent_core.quick_answer", return_value=None):
            r = H.run_scenario(self.SC, "api", 0, TODAY)
        self.assertFalse(r["passed"])
        self.assertIn("model exploded", r["turns"][0]["failures"][0])


if __name__ == "__main__":
    unittest.main()
