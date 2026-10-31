"""Milestone 27 tests: integrity_check and corruption detection (typed errors, never silent)."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402
from minidb.storage.pager import StorageError, _OFF_PAGE_COUNT, _OFF_VERSION  # noqa: E402

PAGE_SIZE = 4096


def build():
    path = os.path.join(tempfile.mkdtemp(), "integrity.db")
    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER, v TEXT)")
    db.run("INSERT INTO t VALUES " + ", ".join(f"({i}, 'v{i}')" for i in range(1, 51)))
    db.run("CREATE INDEX idx_v ON t (v)")
    db.close()
    return path


def flip(path, offset):
    with open(path, "r+b") as handle:
        handle.seek(offset)
        byte = handle.read(1)[0]
        handle.seek(offset)
        handle.write(bytes([byte ^ 0xFF]))


class TestIntegrityCheck(unittest.TestCase):
    def test_healthy_file_reports_ok(self):
        path = build()
        db = Database(path)
        self.assertEqual(db.run("PRAGMA integrity_check")[1], [("ok",)])
        db.close()

    def test_memory_database_reports_ok(self):
        db = Database()
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1)")
        self.assertEqual(db.run("PRAGMA integrity_check")[1], [("ok",)])

    def test_page_count_mismatch_is_reported(self):
        path = build()
        flip(path, _OFF_PAGE_COUNT)
        db = Database(path)
        result = db.run("PRAGMA integrity_check")[1][0][0]
        self.assertNotEqual(result, "ok")
        self.assertIn("pages", result)
        db.close()

    def test_bad_version_is_refused_at_open(self):
        path = build()
        flip(path, _OFF_VERSION)
        with self.assertRaises(StorageError) as ctx:
            Database(path)
        self.assertIn("unsupported format version", str(ctx.exception))

    def test_truncated_file_is_reported(self):
        path = build()
        os.truncate(path, os.path.getsize(path) - 1000)
        with self.assertRaises(StorageError):
            db = Database(path)
            db.run("SELECT count(*) FROM t")


if __name__ == "__main__":
    unittest.main()
