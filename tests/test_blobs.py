"""Milestone 20 tests: blobs in typed columns, blob casts/functions, and 'now' in UTC."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402


class TestBlobAffinity(unittest.TestCase):
    def test_blob_survives_text_affinity(self):
        db = Database()
        db.run("CREATE TABLE t (s TEXT)")
        db.run("INSERT INTO t VALUES (X'0102')")
        self.assertEqual(db.run("SELECT typeof(s), hex(s) FROM t")[1], [("blob", "0102")],
                         "column affinity never changes a blob's storage class")

    def test_blob_survives_integer_affinity(self):
        db = Database()
        db.run("CREATE TABLE t (n INTEGER)")
        db.run("INSERT INTO t VALUES (X'01')")
        self.assertEqual(db.run("SELECT typeof(n) FROM t")[1], [("blob",)])

    def test_text_in_blob_column_stays_text(self):
        db = Database()
        db.run("CREATE TABLE t (b BLOB)")
        db.run("INSERT INTO t VALUES ('hello')")
        self.assertEqual(db.run("SELECT typeof(b), b FROM t")[1], [("text", "hello")])


class TestBlobFunctions(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_cast_blob_to_text_decodes_bytes(self):
        self.assertEqual(self.db.run("SELECT CAST(X'414243' AS TEXT)")[1], [("ABC",)])

    def test_cast_text_to_blob_produces_bytes(self):
        self.assertEqual(self.db.run("SELECT typeof(CAST('abc' AS BLOB)), hex(CAST('abc' AS BLOB))")[1],
                         [("blob", "616263")])

    def test_quote_renders_each_type(self):
        self.assertEqual(self.db.run("SELECT quote(X'0102'), quote('a''b'), quote(NULL), quote(5)")[1],
                         [("X'0102'", "'a''b'", "NULL", "5")])

    def test_concat_treats_blob_as_text(self):
        self.assertEqual(self.db.run("SELECT X'41' || 'b'")[1], [("Ab",)])


class TestNowIsUtc(unittest.TestCase):
    def test_date_now_matches_the_reference(self):
        expected = bc.run_sqlite([["SELECT date('now')"]])
        actual = bc.run_minidb([["SELECT date('now')"]])
        ok, detail = bc.compare(expected, actual)
        self.assertTrue(ok, detail)


class DifferentialM20(unittest.TestCase):
    CASES = [
        ["CREATE TABLE t (s TEXT)", "INSERT INTO t VALUES (X'0102')",
         "SELECT typeof(s), hex(s) FROM t"],
        ["CREATE TABLE t (b BLOB)", "INSERT INTO t VALUES ('hello')",
         "SELECT typeof(b), b FROM t"],
        ["SELECT CAST(X'414243' AS TEXT)"],
        ["SELECT typeof(CAST('abc' AS BLOB)), hex(CAST('abc' AS BLOB))"],
        ["SELECT quote(X'0102'), quote('a''b'), quote(NULL), quote(5)"],
        ["SELECT X'41' || 'b', length(X'4142')"],
        ["SELECT date('now')"],
        ["CREATE TABLE t (v BLOB)", "INSERT INTO t VALUES (X'DEADBEEF')",
         "SELECT typeof(v), length(v), hex(v) FROM t"],
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
