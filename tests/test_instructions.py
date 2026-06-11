"""Tests for the standing-instructions registry.

Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import instructions as ins


class TempRegistryMixin:
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self._p = patch.object(ins, 'INSTRUCTIONS_PATH', self.path)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        if os.path.exists(self.path):
            os.unlink(self.path)


class TestRegistry(TempRegistryMixin, unittest.TestCase):
    def test_add_renders_into_prompt_block(self):
        ins.add_standing_instruction.invoke({'instruction': 'Always use metric units.'})
        block = ins.render_for_prompt()
        self.assertIn('1. Always use metric units.', block)

    def test_ids_increment_and_survive_removal(self):
        ins.add_standing_instruction.invoke({'instruction': 'rule A'})
        ins.add_standing_instruction.invoke({'instruction': 'rule B'})
        ins.remove_standing_instruction.invoke({'instruction_id': 1})
        msg = ins.add_standing_instruction.invoke({'instruction': 'rule C'})
        self.assertIn('#3', msg)              # ids never reused
        block = ins.render_for_prompt()
        self.assertNotIn('rule A', block)
        self.assertIn('rule B', block)
        self.assertIn('rule C', block)

    def test_update_rewrites_in_place(self):
        ins.add_standing_instruction.invoke({'instruction': 'old wording'})
        ins.update_standing_instruction.invoke({'instruction_id': 1, 'instruction': 'new wording'})
        block = ins.render_for_prompt()
        self.assertIn('new wording', block)
        self.assertNotIn('old wording', block)

    def test_unknown_id_messages(self):
        self.assertIn('No standing instruction',
                      ins.update_standing_instruction.invoke({'instruction_id': 9, 'instruction': 'x'}))
        self.assertIn('No standing instruction',
                      ins.remove_standing_instruction.invoke({'instruction_id': 9}))

    def test_empty_registry_renders_placeholder(self):
        self.assertEqual(ins.render_for_prompt(), '(none yet)')


if __name__ == '__main__':
    unittest.main()
