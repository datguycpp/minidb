"""Milestone 19 tests: VACUUM, overflow pages and blob literals (t3a's observable surface)."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402


class TestBlobs(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_blob_literal_type(self):
        self.assertEqual(self.db.run("SELECT typeof(X'0102'), length(X'0102')")[1], [("blob", 2)])

    def test_hex_round_trip(self):
        self.assertEqual(self.db.run("SELECT hex(X'00ff')")[1], [("00FF",)])

    def test_blob_stored_in_column(self):
        self.db.run("CREATE TABLE t (v BLOB)")
        self.db.run("INSERT INTO t VALUES (X'DEADBEEF')")
        self.assertEqual(self.db.run("SELECT typeof(v), hex(v) FROM t")[1], [("blob", "DEADBEEF")])


class TestOverflow(unittest.TestCase):
    def test_body_larger_than_a_page_round_trips(self):
        path = os.path.join(tempfile.mkdtemp(), "ov.db")
        body = "x" * 5000
        db = Database(path)
        db.run("CREATE TABLE t (id INTEGER, body TEXT)")
        db.run(f"INSERT INTO t VALUES (1, '{body}')")
        self.assertEqual(db.run("SELECT length(body) FROM t")[1], [(5000,)])
        db.close()
        again = Database(path)
        self.assertEqual(again.run("SELECT length(body) FROM t")[1], [(5000,)],
                         "the overflow chain must survive reopen")
        self.assertEqual(again.run("SELECT substr(body, 1, 4) FROM t")[1], [("xxxx",)])
        again.close()

    def test_many_large_rows(self):
        path = os.path.join(tempfile.mkdtemp(), "ov2.db")
        db = Database(path)
        db.run("CREATE TABLE t (id INTEGER, body TEXT)")
        db.run("BEGIN")
        for i in range(1, 11):
            db.run(f"INSERT INTO t VALUES ({i}, '{'y' * 3000}')")
        db.run("COMMIT")
        db.close()
        again = Database(path)
        self.assertEqual(again.run("SELECT count(*), min(length(body)) FROM t")[1], [(10, 3000)])
        again.close()


class TestVacuum(unittest.TestCase):
    def test_vacuum_reclaims_space_and_keeps_rows(self):
        path = os.path.join(tempfile.mkdtemp(), "v.db")
        db = Database(path)
        db.run("CREATE TABLE t (id INTEGER, body TEXT)")
        db.run("BEGIN")
        for start in range(1, 201, 50):
            block = ", ".join(f"({i}, '{'x' * 50}')" for i in range(start, start + 50))
            db.run(f"INSERT INTO t VALUES {block}")
        db.run("COMMIT")
        db.run("CREATE INDEX idx_id ON t (id)")
        db.run("DELETE FROM t WHERE id > 20")
        grown = os.path.getsize(path)
        db.run("VACUUM")
        shrunk = os.path.getsize(path)
        self.assertLess(shrunk, grown, "VACUUM must return freed pages")
        self.assertEqual(db.run("SELECT count(*) FROM t")[1], [(20,)])
        self.assertEqual(db.run("SELECT id FROM t WHERE id = 5")[1], [(5,)],
                         "the index must still serve after the rebuild")
        db.close()
        again = Database(path)
        self.assertEqual(again.run("SELECT count(*), max(id) FROM t")[1], [(20, 20)],
                         "vacuumed state persists through reopen")
        again.close()

    def test_vacuum_on_memory_database_is_a_no_op(self):
        db = Database()
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1)")
        self.assertIsNone(db.run("VACUUM"))
        self.assertEqual(db.run("SELECT count(*) FROM t")[1], [(1,)])


class DifferentialM19(unittest.TestCase):
    CASES = [
        ["SELECT typeof(X'0102'), length(X'0102'), hex(X'0102')"],
        ["SELECT hex('ab'), typeof('ab')"],
        ["CREATE TABLE t (v BLOB)", "INSERT INTO t VALUES (X'00FF')",
         "SELECT typeof(v), length(v), hex(v) FROM t"],
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1), (2)",
         "VACUUM", "SELECT count(*) FROM t"],
    ]

    def test_cases_match_sqlite(self):
        for statements in self.CASES:
            with self.subTest(sql=statements[-1]):
                expected = bc.run_sqlite([statements])
                actual = bc.run_minidb([statements])
                ok, detail = bc.compare(expected, actual)
                self.assertTrue(ok, f"{statements[-1]} -> {detail}")


if __name__ == "__main__":
    unittest.main()
