"""Milestone 25/26 tests: single-writer locking and transaction snapshots.

The model is stated rather than implied: one handle owns the database file for its lifetime (SQLite's
single-writer model), a second opener waits and then reports the reference's wording, and a
transaction inside a handle sees its own writes until it rolls back or commits.
"""

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database, ExecError  # noqa: E402


class TestSingleWriterLock(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "lock.db")
        self.db = Database(self.path)
        self.db.run("CREATE TABLE t (a INTEGER)")
        self.db.run("INSERT INTO t VALUES (1)")
        os.environ["MINIDB_LOCK_TIMEOUT"] = "0.2"

    def tearDown(self):
        os.environ.pop("MINIDB_LOCK_TIMEOUT", None)
        try:
            self.db.close()
        except Exception:
            pass

    def test_second_handle_is_refused_with_the_reference_wording(self):
        started = time.perf_counter()
        with self.assertRaises(ExecError) as ctx:
            Database(self.path)
        self.assertEqual(str(ctx.exception), "database is locked")
        self.assertLess(time.perf_counter() - started, 2.0, "the wait is bounded, not a hang")

    def test_first_handle_keeps_working_while_the_second_waits(self):
        try:
            Database(self.path)
        except ExecError:
            pass
        self.assertEqual(self.db.run("SELECT a FROM t")[1], [(1,)])

    def test_lock_is_released_on_close(self):
        self.db.close()
        again = Database(self.path)
        self.assertEqual(again.run("SELECT a FROM t")[1], [(1,)])
        again.close()


class TestTransactionSnapshot(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "snap.db")
        self.db = Database(self.path)
        self.db.run("CREATE TABLE t (a INTEGER)")
        self.db.run("INSERT INTO t VALUES (1)")

    def tearDown(self):
        self.db.close()

    def test_uncommitted_writes_are_visible_inside_the_transaction_only(self):
        self.db.run("BEGIN")
        self.db.run("INSERT INTO t VALUES (2)")
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(2,)])
        self.db.run("ROLLBACK")
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(1,)])

    def test_committed_writes_survive_a_reopen(self):
        self.db.run("BEGIN")
        self.db.run("INSERT INTO t VALUES (2)")
        self.db.run("COMMIT")
        self.db.close()
        again = Database(self.path)
        self.assertEqual(again.run("SELECT count(*) FROM t")[1], [(2,)])
        again.close()


if __name__ == "__main__":
    unittest.main()
