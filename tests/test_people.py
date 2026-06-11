"""Tests for the people model — upsert, alias resolution, birthday automation.

Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import people


class TempPeopleMixin:
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self._p = patch.object(people, 'PEOPLE_PATH', self.path)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        if os.path.exists(self.path):
            os.unlink(self.path)


class TestPeople(TempPeopleMixin, unittest.TestCase):
    def test_create_then_update_same_person(self):
        people.remember_person.invoke({'name': 'Priya', 'relation': 'girlfriend',
                                       'alias': 'my girlfriend'})
        people.remember_person.invoke({'name': 'priya', 'note': 'loves tulips'})
        roster = people.roster_for_prompt()
        self.assertEqual(roster, "Priya (girlfriend)")        # one record, not two
        dossier = people.get_person.invoke({'name': 'Priya'})
        self.assertIn('tulips', dossier)
        self.assertIn('my girlfriend', dossier)

    def test_alias_resolves_to_person(self):
        people.remember_person.invoke({'name': 'Priya', 'alias': 'my girlfriend'})
        dossier = people.get_person.invoke({'name': 'My Girlfriend'})
        self.assertIn('Priya', dossier)

    def test_birthday_creates_yearly_commitment_once(self):
        with patch('skills.commitment_manager.get_open_commitments', return_value=[]), \
             patch('skills.commitment_manager.add', return_value=9) as add:
            people.remember_person.invoke({'name': 'Priya', 'birthday_iso': '1999-06-28'})
        add.assert_called_once()
        kwargs = add.call_args.kwargs
        self.assertEqual(kwargs['kind'], 'people_date')
        self.assertEqual(kwargs['recurring'], 'yearly')
        self.assertTrue(kwargs['due_date'].endswith('-06-28'))

    def test_birthday_commitment_not_duplicated(self):
        existing = [{'kind': 'people_date', 'who': 'Priya'}]
        with patch('skills.commitment_manager.get_open_commitments', return_value=existing), \
             patch('skills.commitment_manager.add') as add:
            people.remember_person.invoke({'name': 'Priya', 'birthday_iso': '1999-06-28'})
        add.assert_not_called()

    def test_next_birthday_rolls_to_next_year_when_passed(self):
        today = datetime(2026, 7, 1)
        self.assertEqual(people._next_birthday_occurrence('1999-06-28', today), '2027-06-28')
        self.assertEqual(people._next_birthday_occurrence('1999-12-25', today), '2026-12-25')

    def test_unknown_person(self):
        self.assertIn("don't have a record",
                      people.get_person.invoke({'name': 'Stranger'}))


if __name__ == '__main__':
    unittest.main()
