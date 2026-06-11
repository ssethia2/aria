"""Tests for the notes store — create, append, search, safe read.

Run: python3 -m unittest discover tests
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from skills import notes_manager as nm


class TempNotesMixin:
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._p = patch.object(nm, 'NOTES_DIR', self.dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


class TestNotes(TempNotesMixin, unittest.TestCase):
    def test_create_and_search(self):
        nm.create_note.invoke({'title': 'Travel Ideas', 'content': 'Kyoto in autumn'})
        out = nm.search_notes.invoke({'query': 'kyoto'})
        self.assertIn('travel-ideas.md', out)

    def test_search_requires_all_terms(self):
        nm.create_note.invoke({'title': 'Groceries', 'content': 'milk and eggs'})
        self.assertIn('groceries', nm.search_notes.invoke({'query': 'milk eggs'}).lower())
        self.assertIn('No notes match', nm.search_notes.invoke({'query': 'milk caviar'}))

    def test_append_creates_when_missing(self):
        msg = nm.append_to_note.invoke({'title_query': 'Reading List', 'content': 'Dune'})
        self.assertIn('created', msg.lower())
        nm.append_to_note.invoke({'title_query': 'reading', 'content': 'Neuromancer'})
        body = nm.read_note.invoke({'filename': 'reading-list.md'})
        self.assertIn('Dune', body)
        self.assertIn('Neuromancer', body)

    def test_duplicate_titles_get_distinct_files(self):
        nm.create_note.invoke({'title': 'Notes', 'content': 'a'})
        nm.create_note.invoke({'title': 'Notes', 'content': 'b'})
        files = sorted(os.listdir(self.dir))
        self.assertEqual(files, ['notes-2.md', 'notes.md'])

    def test_read_rejects_path_traversal(self):
        out = nm.read_note.invoke({'filename': '../../etc/passwd'})
        self.assertIn('No note at', out)

    def test_imported_subdir_searchable(self):
        nm.write_note_file('Old Trip', 'Lisbon notes', subdir='imported')
        out = nm.search_notes.invoke({'query': 'lisbon'})
        self.assertIn('imported/old-trip.md', out)


if __name__ == '__main__':
    unittest.main()
