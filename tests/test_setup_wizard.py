"""Tests for the first-run wizard's env-rendering (the testable core).

Run: python3 -m unittest discover tests
"""
import unittest

import setup_wizard as wiz


class TestRenderEnv(unittest.TestCase):
    TEMPLATE = [
        "# comment line\n",
        "TELEGRAM_BOT_TOKEN=\n",
        "ANTHROPIC_API_KEY=\n",
        "# another comment\n",
        "GEMINI_API_KEY=\n",
    ]

    def test_fills_provided_keys_keeps_comments(self):
        out = wiz.render_env(self.TEMPLATE, {
            'TELEGRAM_BOT_TOKEN': '123:abc', 'ANTHROPIC_API_KEY': 'sk-ant'})
        self.assertIn("TELEGRAM_BOT_TOKEN=123:abc", out)
        self.assertIn("ANTHROPIC_API_KEY=sk-ant", out)
        self.assertIn("# comment line", out)
        self.assertIn("# another comment", out)

    def test_unprovided_key_stays_blank(self):
        out = wiz.render_env(self.TEMPLATE, {'TELEGRAM_BOT_TOKEN': 'x'})
        self.assertIn("GEMINI_API_KEY=\n", out)

    def test_does_not_touch_comment_lines_with_equals(self):
        out = wiz.render_env(["# KEY=ignore me\n", "REAL=\n"], {'REAL': 'v'})
        self.assertIn("# KEY=ignore me", out)
        self.assertIn("REAL=v", out)


class TestExistingValues(unittest.TestCase):
    def test_parses_keys_skips_comments(self):
        import os, tempfile
        fd, path = tempfile.mkstemp()
        os.close(fd)
        with open(path, 'w') as f:
            f.write("# c\nFOO=bar\nBAZ=qux\n#X=y\n")
        try:
            vals = wiz._existing_values(path)
        finally:
            os.unlink(path)
        self.assertEqual(vals, {'FOO': 'bar', 'BAZ': 'qux'})


if __name__ == '__main__':
    unittest.main()
