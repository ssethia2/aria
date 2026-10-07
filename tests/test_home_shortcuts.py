"""Apple Shortcuts home bridge: prefix gate (the safety boundary), name resolution, failure
modes. Offline — the `shortcuts` CLI is mocked. Run: python3 -m unittest discover tests
"""
import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from skills import home_shortcuts as hs

ALL = "Open Note\nAria Lights Off\nAria Lights On\nAria Movie Mode\nMake GIF\nMy Private Thing\n"


def _cli(list_out=ALL, run=None):
    """subprocess.run fake: `shortcuts list` -> list_out; `shortcuts run X` -> run result."""
    calls = []

    def fake(args, **kw):
        calls.append(args)
        if args[:2] == ["shortcuts", "list"]:
            return MagicMock(returncode=0, stdout=list_out, stderr="")
        return run or MagicMock(returncode=0, stdout="", stderr="")

    return fake, calls


class TestPrefixGate(unittest.TestCase):
    def test_only_prefixed_shortcuts_are_visible(self):
        fake, _ = _cli()
        with patch.object(hs.subprocess, "run", fake):
            out = hs.list_home_shortcuts.invoke({})
        self.assertIn("Aria Lights Off", out)
        self.assertIn("Aria Movie Mode", out)
        for hidden in ("Open Note", "Make GIF", "My Private Thing"):
            self.assertNotIn(hidden, out)           # the rest of the Mac is invisible

    def test_cannot_run_a_non_prefixed_shortcut_even_by_exact_name(self):
        fake, calls = _cli()
        with patch.object(hs.subprocess, "run", fake):
            out = hs.run_home_shortcut.invoke({"name": "Open Note"})
        self.assertIn("No home shortcut", out)
        self.assertFalse(any(c[:2] == ["shortcuts", "run"] for c in calls))  # never executed

    def test_custom_prefix_via_env(self):
        fake, _ = _cli("Home Porch\nAria Lights Off\n")
        with patch.dict(os.environ, {"ARIA_SHORTCUT_PREFIX": "Home"}), \
             patch.object(hs.subprocess, "run", fake):
            out = hs.list_home_shortcuts.invoke({})
        self.assertIn("Home Porch", out)
        self.assertNotIn("Aria Lights Off", out)


class TestResolveAndRun(unittest.TestCase):
    def test_partial_name_resolves_and_runs_with_arg_list_not_shell(self):
        fake, calls = _cli()
        with patch.object(hs.subprocess, "run", fake):
            out = hs.run_home_shortcut.invoke({"name": "movie mode"})
        self.assertIn("Ran 'Aria Movie Mode'", out)
        run_call = next(c for c in calls if c[:2] == ["shortcuts", "run"])
        self.assertEqual(run_call, ["shortcuts", "run", "Aria Movie Mode"])  # list, no shell

    def test_ambiguous_partial_asks_which(self):
        fake, calls = _cli()
        with patch.object(hs.subprocess, "run", fake):
            out = hs.run_home_shortcut.invoke({"name": "lights"})   # On + Off both match
        self.assertIn("which one", out)
        self.assertFalse(any(c[:2] == ["shortcuts", "run"] for c in calls))

    def test_exact_match_beats_substring(self):
        fake, _ = _cli("Aria Lights\nAria Lights Off\n")
        with patch.object(hs.subprocess, "run", fake):
            out = hs.run_home_shortcut.invoke({"name": "aria lights"})
        self.assertIn("Ran 'Aria Lights'", out)

    def test_shell_metacharacters_in_name_never_execute(self):
        fake, calls = _cli()
        with patch.object(hs.subprocess, "run", fake):
            out = hs.run_home_shortcut.invoke({"name": "x; rm -rf ~"})
        self.assertIn("No home shortcut", out)
        self.assertFalse(any(c[:2] == ["shortcuts", "run"] for c in calls))

    def test_shortcut_output_is_returned(self):
        fake, _ = _cli(run=MagicMock(returncode=0, stdout="3 lights toggled\n", stderr=""))
        with patch.object(hs.subprocess, "run", fake):
            out = hs.run_home_shortcut.invoke({"name": "Aria Lights Off"})
        self.assertIn("3 lights toggled", out)


class TestFailureModes(unittest.TestCase):
    def test_no_shortcuts_yet_explains_how_to_make_them(self):
        fake, _ = _cli("Open Note\nMake GIF\n")
        with patch.object(hs.subprocess, "run", fake):
            out = hs.list_home_shortcuts.invoke({})
        self.assertIn("No home shortcuts set up yet", out)
        self.assertIn("Aria Lights Off", out)

    def test_non_macos_cli_missing(self):
        with patch.object(hs.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIn("can't reach Apple Shortcuts",
                          hs.list_home_shortcuts.invoke({}))
            self.assertIn("can't reach Apple Shortcuts",
                          hs.run_home_shortcut.invoke({"name": "x"}))

    def test_run_failure_surfaces_the_error(self):
        fake, _ = _cli(run=MagicMock(returncode=1, stdout="", stderr="Home access denied"))
        with patch.object(hs.subprocess, "run", fake):
            out = hs.run_home_shortcut.invoke({"name": "Aria Lights Off"})
        self.assertIn("failed", out)
        self.assertIn("Home access denied", out)

    def test_hung_permission_dialog_times_out_with_a_hint(self):
        def fake(args, **kw):
            if args[:2] == ["shortcuts", "list"]:
                return MagicMock(returncode=0, stdout=ALL, stderr="")
            raise subprocess.TimeoutExpired(args, kw.get("timeout", 45))
        with patch.object(hs.subprocess, "run", fake):
            out = hs.run_home_shortcut.invoke({"name": "Aria Lights Off"})
        self.assertIn("permission", out)            # tells him to look at the Mac screen


class TestWiring(unittest.TestCase):
    def test_owner_has_it_guest_does_not(self):
        import agent_core
        owner = {t.name for t in agent_core.build_tools(guest=False)}
        guest = {t.name for t in agent_core.build_tools(guest=True)}
        self.assertTrue({"list_home_shortcuts", "run_home_shortcut"} <= owner)
        self.assertFalse({"list_home_shortcuts", "run_home_shortcut"} & guest)


if __name__ == "__main__":
    unittest.main()
