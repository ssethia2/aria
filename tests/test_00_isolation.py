"""The guard in tests/__init__.py: unit tests cannot write the owner's real state.

Named to sort first, so a plain `python3 -m unittest discover tests` installs the guard
before any other test module is imported.
"""
import os
import sqlite3
import unittest

import tests  # noqa: F401  (installs the guard)


class TestRealStateIsOutOfReach(unittest.TestCase):
    def setUp(self):
        self.before = list(tests.VIOLATIONS)

    def tearDown(self):
        tests.VIOLATIONS[:] = self.before   # these tests trip the guard on purpose

    def test_writing_a_repo_file_raises(self):
        target = os.path.join(tests.REPO, "cost_log.json")
        for mode in ("w", "a", "r+"):
            with self.assertRaises(tests.RealStateWrite):
                open(target, mode)

    def test_reading_is_allowed(self):
        with open(os.path.join(tests.REPO, "requirements.txt")) as f:
            self.assertTrue(f.read())

    def test_opening_a_repo_database_raises(self):
        with self.assertRaises(tests.RealStateWrite):
            sqlite3.connect(os.path.join(tests.REPO, "aria_calendar.db"))

    def test_deleting_or_replacing_a_repo_file_raises(self):
        target = os.path.join(tests.REPO, "engine_state.json")
        with self.assertRaises(tests.RealStateWrite):
            os.remove(target)
        with self.assertRaises(tests.RealStateWrite):
            os.replace(os.path.join(tests.SANDBOX, "x"), target)

    def test_temp_files_and_memory_databases_still_work(self):
        path = os.path.join(tests.SANDBOX, "scratch.txt")
        with open(path, "w") as f:
            f.write("ok")
        os.remove(path)
        sqlite3.connect(":memory:").close()

    def test_state_paths_point_at_the_sandbox(self):
        import cost_tracker
        import engine
        import memory
        from skills import commitment_manager
        for path in (cost_tracker.LOG_PATH, engine.STATE_PATH, memory.SCRATCHPAD_PATH,
                     memory.CONVERSATION_PATH, commitment_manager.DB_PATH):
            self.assertTrue(os.path.realpath(path).startswith(tests.SANDBOX), path)

    def test_the_network_is_out_of_reach(self):
        import socket
        s = socket.socket()
        try:
            with self.assertRaises(tests.RealStateWrite):
                s.connect(("api.telegram.org", 443))
        finally:
            s.close()

    def test_the_real_memory_store_is_detached(self):
        import memory
        self.assertIsNone(memory.collection)

    def test_a_swallowed_violation_is_still_recorded(self):
        try:
            open(os.path.join(tests.REPO, "people.json"), "w")
        except AssertionError:
            pass
        self.assertEqual(len(tests.VIOLATIONS), len(self.before) + 1)


if __name__ == "__main__":
    unittest.main()
