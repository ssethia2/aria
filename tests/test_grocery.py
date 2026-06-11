"""Tests for the grocery list store.

Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from skills import grocery_manager as gm


class TempListMixin:
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self._p = patch.object(gm, 'LIST_PATH', self.path)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        if os.path.exists(self.path):
            os.unlink(self.path)


class TestGrocery(TempListMixin, unittest.TestCase):
    def test_add_splits_on_commas_and_newlines(self):
        gm.add_to_grocery_list.invoke({'items': 'milk, eggs\n2 lbs chicken thighs'})
        out = gm.view_grocery_list.invoke({})
        self.assertIn('milk', out)
        self.assertIn('eggs', out)
        self.assertIn('2 lbs chicken thighs', out)

    def test_dedups_case_insensitively(self):
        gm.add_to_grocery_list.invoke({'items': 'Milk'})
        msg = gm.add_to_grocery_list.invoke({'items': 'milk, bread'})
        self.assertIn('Already on the list: milk', msg)
        self.assertIn('Added: bread', msg)
        self.assertEqual(gm.view_grocery_list.invoke({}).count('- '), 2)

    def test_remove_matches_substring(self):
        gm.add_to_grocery_list.invoke({'items': '2 lbs chicken thighs, milk'})
        msg = gm.remove_from_grocery_list.invoke({'item': 'chicken'})
        self.assertIn('Removed 1', msg)
        self.assertNotIn('chicken', gm.view_grocery_list.invoke({}))

    def test_clear_empties_list(self):
        gm.add_to_grocery_list.invoke({'items': 'a, b, c'})
        gm.clear_grocery_list.invoke({})
        self.assertIn('empty', gm.view_grocery_list.invoke({}).lower())

    def test_recipe_batch_add_via_helper(self):
        # Simulates Aria extracting ingredients then adding them in one call.
        added, dup = gm.add_items(['onion', 'garlic', 'canned tomatoes', 'cream', 'garlic'])
        self.assertEqual(added, ['onion', 'garlic', 'canned tomatoes', 'cream'])
        self.assertEqual(dup, ['garlic'])  # in-batch dedup

    def test_empty_add(self):
        self.assertIn('Nothing', gm.add_to_grocery_list.invoke({'items': '  '}))


if __name__ == '__main__':
    unittest.main()
