"""Milestone 18 tests: the page cache must be invisible to results, and must not survive a
rollback or a close with stale bytes."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402


class TestPageCache(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "cache.db")

    def test_reads_see_uncommitted_writes(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("BEGIN")
        db.run("INSERT INTO t VALUES (1), (2)")
        self.assertEqual(db.run("SELECT count(*) FROM t")[1], [(2,)],
                         "the dirty page must win over the cached clean copy")
        db.run("ROLLBACK")
        db.close()

    def test_rollback_does_not_leave_cached_rows(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1)")
        db.run("BEGIN")
        db.run("INSERT INTO t VALUES (999)")
        self.assertEqual(db.run("SELECT count(*) FROM t")[1], [(2,)])
        db.run("ROLLBACK")
        self.assertEqual(db.run("SELECT count(*) FROM t")[1], [(1,)])
        self.assertEqual(db.run("SELECT a FROM t")[1], [(1,)])
        db.close()

    def test_reopen_sees_committed_state(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1), (2), (3)")
        db.close()
        again = Database(self.path)
        self.assertEqual(again.run("SELECT a FROM t ORDER BY a")[1], [(1,), (2,), (3,)])
        again.close()

    def test_updates_and_deletes_are_consistent_after_reopen(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1), (2), (3)")
        db.run("UPDATE t SET a = a * 10 WHERE a > 1")
        db.run("DELETE FROM t WHERE a = 1")
        db.close()
        again = Database(self.path)
        self.assertEqual(again.run("SELECT a FROM t ORDER BY a")[1], [(20,), (30,)])
        again.close()

    def test_large_scan_matches_row_count(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
        db.run("BEGIN")
        for start in range(1, 3001, 100):
            block = ", ".join(f"({i}, {i % 7})" for i in range(start, start + 100))
            db.run(f"INSERT INTO t VALUES {block}")
        db.run("COMMIT")
        self.assertEqual(db.run("SELECT count(*), sum(v) FROM t")[1],
                         [(3000, sum(i % 7 for i in range(1, 3001)))])
        db.close()


if __name__ == "__main__":
    unittest.main()
