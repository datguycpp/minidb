"""Milestone 25/26 tests: shared-lock readers, exclusive writer, and transaction snapshots.

Model (stated, not implied): readers hold LOCK_SH and can coexist; a writer holds LOCK_EX and waits
for open readers up to MINIDB_LOCK_TIMEOUT, then fails with SQLite's wording. A read-only handle
that tries to write is refused with the reference's message — upgrades are a typed refusal, not a
silent wait, because upgrade paths are where lock protocols deadlock.
"""

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database, ExecError  # noqa: E402


class TestReaderWriterLocks(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "lock.db")
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1)")
        db.close()
        os.environ["MINIDB_LOCK_TIMEOUT"] = "0.2"

    def tearDown(self):
        os.environ.pop("MINIDB_LOCK_TIMEOUT", None)

    def test_two_readers_coexist(self):
        first = Database(self.path, readonly=True)
        second = Database(self.path, readonly=True)
        self.assertEqual(first.run("SELECT a FROM t")[1], [(1,)])
        self.assertEqual(second.run("SELECT a FROM t")[1], [(1,)])
        first.close()
        second.close()

    def test_writer_waits_for_open_readers_then_reports_the_reference_wording(self):
        reader = Database(self.path, readonly=True)
        started = time.perf_counter()
        with self.assertRaises(ExecError) as ctx:
            Database(self.path)
        self.assertEqual(str(ctx.exception), "database is locked")
        self.assertLess(time.perf_counter() - started, 2.0, "the wait is bounded, not a hang")
        reader.close()

    def test_writer_proceeds_once_readers_close(self):
        reader = Database(self.path, readonly=True)
        reader.close()
        writer = Database(self.path)
        self.assertEqual(writer.run("SELECT a FROM t")[1], [(1,)])
        writer.close()

    def test_readonly_handle_refuses_writes(self):
        reader = Database(self.path, readonly=True)
        with self.assertRaises(ExecError) as ctx:
            reader.run("INSERT INTO t VALUES (2)")
        self.assertEqual(str(ctx.exception), "attempt to write a readonly database")
        reader.close()

    def test_lock_is_released_on_close(self):
        db = Database(self.path)
        db.close()
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
