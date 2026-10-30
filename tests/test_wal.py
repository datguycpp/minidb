"""Milestone 7 tests: write-ahead log and crash recovery.

The interesting cases are the ones where the process dies. Those run in a subprocess with
MINIDB_CRASH_POINT set, so the "crash" is a real `os._exit` in a real process rather than an
exception we caught ourselves.
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402

PYTHON = sys.executable


def run_child(script, env_extra=None):
    env = {**os.environ, "PYTHONPATH": ROOT}
    env.update(env_extra or {})
    return subprocess.run([PYTHON, "-c", script], capture_output=True, text=True, env=env, timeout=30)


class WalBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "wal.db")

    def tearDown(self):
        self.dir.cleanup()

    def rows(self):
        db = Database(self.path)
        try:
            return db.run("SELECT * FROM t")[1]
        finally:
            db.close()


class TestWalFiles(WalBase):
    def test_wal_is_truncated_after_clean_commit(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1)")
        db.close()
        wal = self.path + ".wal"
        self.assertTrue(os.path.exists(wal))
        self.assertEqual(os.path.getsize(wal), 0, "a clean commit must leave no pending log")

    def test_uncommitted_pages_never_reach_the_file(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("BEGIN")
        db.run("INSERT INTO t VALUES (1)")
        size_during = os.path.getsize(self.path)
        db.run("ROLLBACK")
        self.assertEqual(os.path.getsize(self.path), size_during)
        db.close()
        self.assertEqual(self.rows(), [])


class TestCrashRecovery(WalBase):
    def test_crash_after_wal_sync_recovers_the_commit(self):
        script = f"""
import sys
sys.path.insert(0, {ROOT!r})
from minidb.engine import Database
db = Database({self.path!r})
db.run('CREATE TABLE t (a INTEGER)')
db.run('INSERT INTO t VALUES (42)')
"""
        result = run_child(script, {"MINIDB_CRASH_POINT": "after_wal", "MINIDB_CRASH_COMMIT": "3"})
        self.assertEqual(result.returncode, 9, "the child was supposed to die, not finish")
        self.assertEqual(self.rows(), [(42,)], "a commit synced to the log must survive the crash")

    def test_crash_mid_apply_recovers_the_commit(self):
        script = f"""
import sys
sys.path.insert(0, {ROOT!r})
from minidb.engine import Database
db = Database({self.path!r})
db.run('CREATE TABLE t (a INTEGER)')
db.run('INSERT INTO t VALUES (7)')
"""
        result = run_child(script, {"MINIDB_CRASH_POINT": "mid_apply", "MINIDB_CRASH_COMMIT": "3"})
        self.assertEqual(result.returncode, 9)
        self.assertEqual(self.rows(), [(7,)])

    def test_uncommitted_transaction_is_lost_not_partially_applied(self):
        script = f"""
import os, sys
sys.path.insert(0, {ROOT!r})
from minidb.engine import Database
db = Database({self.path!r})
db.run('CREATE TABLE t (a INTEGER)')
db.run('INSERT INTO t VALUES (1)')
db.run('BEGIN')
db.run('INSERT INTO t VALUES (2)')
os._exit(9)
"""
        result = run_child(script)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(self.rows(), [(1,)], "the committed row survives; the open transaction does not")

    def test_torn_wal_tail_is_discarded(self):
        script = f"""
import sys
sys.path.insert(0, {ROOT!r})
from minidb.engine import Database
db = Database({self.path!r})
db.run('CREATE TABLE t (a INTEGER)')
db.run('INSERT INTO t VALUES (9)')
"""
        run_child(script, {"MINIDB_CRASH_POINT": "after_wal", "MINIDB_CRASH_COMMIT": "3"})
        with open(self.path + ".wal", "ab") as wal:
            wal.write(b"TORN-TAIL-BYTE-GARBAGE")
        self.assertEqual(self.rows(), [(9,)], "a valid batch must be replayed even with junk after it")

    def test_corrupted_frame_is_not_replayed(self):
        script = f"""
import sys
sys.path.insert(0, {ROOT!r})
from minidb.engine import Database
db = Database({self.path!r})
db.run('CREATE TABLE t (a INTEGER)')
db.run('INSERT INTO t VALUES (5)')
"""
        run_child(script, {"MINIDB_CRASH_POINT": "after_wal", "MINIDB_CRASH_COMMIT": "3"})
        wal = self.path + ".wal"
        with open(wal, "r+b") as fh:
            blob = bytearray(fh.read())
            blob[20] ^= 0xFF            # corrupt one byte inside the first frame's page payload
            fh.seek(0)
            fh.write(blob)
        rows = self.rows()
        self.assertEqual(rows, [], "a frame whose checksum fails must not be applied")

    def test_crash_during_ddl_does_not_half_create_a_table(self):
        script = f"""
import sys
sys.path.insert(0, {ROOT!r})
from minidb.engine import Database
db = Database({self.path!r})
db.run('CREATE TABLE t (a INTEGER)')
"""
        run_child(script, {"MINIDB_CRASH_POINT": "after_wal", "MINIDB_CRASH_COMMIT": "2"})
        db = Database(self.path)
        try:
            self.assertEqual(db.run("SELECT * FROM t"), (["a"], []),
                             "the CREATE was committed to the log, so recovery must land it whole")
        finally:
            db.close()


class TestRepeatedCrashes(WalBase):
    def test_many_crashes_keep_every_committed_row(self):
        script = f"""
import os, sys
sys.path.insert(0, {ROOT!r})
from minidb.engine import Database
db = Database({self.path!r})
db.run('INSERT INTO t VALUES ({{n}})')
os._exit(9)
"""
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.close()

        for n in range(1, 11):
            # Each child commits one row and dies immediately after that commit.
            result = run_child(script.format(n=n),
                               {"MINIDB_CRASH_POINT": "after_wal", "MINIDB_CRASH_COMMIT": "2"})
            self.assertEqual(result.returncode, 9, result.stderr)

        rows = self.rows()
        self.assertEqual(rows, [(n,) for n in range(1, 11)])


if __name__ == "__main__":
    unittest.main()
