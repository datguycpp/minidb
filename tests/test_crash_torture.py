"""Milestone 24 tests: crash-torture invariants (the checker must be able to fail)."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import crash_torture  # noqa: E402
from minidb.engine import Database  # noqa: E402


class TestInvariantChecker(unittest.TestCase):
    def test_contiguous_prefix_passes(self):
        path = os.path.join(tempfile.mkdtemp(), "ok.db")
        db = Database(path)
        db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
        db.run("CREATE INDEX idx_v ON t (v)")
        db.run("INSERT INTO t VALUES (1, 1), (2, 2), (3, 3)")
        db.close()
        self.assertIsNone(crash_torture.check(path))

    def test_a_gap_is_reported(self):
        path = os.path.join(tempfile.mkdtemp(), "gap.db")
        db = Database(path)
        db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
        db.run("CREATE INDEX idx_v ON t (v)")
        db.run("INSERT INTO t VALUES (1, 1), (3, 3)")
        db.close()
        problem = crash_torture.check(path)
        self.assertIsNotNone(problem)
        self.assertIn("contiguous", problem)

    def test_a_lying_index_is_reported(self):
        db = Database()
        db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
        db.run("CREATE INDEX idx_v ON t (v)")
        db.run("INSERT INTO t VALUES (1, 1), (2, 1), (3, 2)")
        # Remove a row from the index without touching the table: exactly the "lying index" shape.
        index = list(db.tables["t"].indexes.values())[0]
        index.remove(1, (1, 1))
        problem = crash_torture.check_database(db)
        self.assertIsNotNone(problem)
        self.assertIn("disagrees", problem)


class TestCrashIteration(unittest.TestCase):
    def test_killed_child_recovers_to_a_prefix(self):
        path = os.path.join(tempfile.mkdtemp(), "crash.db")
        result = crash_torture.run_iteration(path, seed=7, rows=10)
        self.assertIn(result.returncode, (0, 9))
        self.assertIsNone(crash_torture.check(path), "recovery must leave a consistent prefix")


if __name__ == "__main__":
    unittest.main()
