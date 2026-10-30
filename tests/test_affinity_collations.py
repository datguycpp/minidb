"""Milestone 17 tests: type affinity and collations."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError, affinity_of  # noqa: E402


class TestAffinityRules(unittest.TestCase):
    def test_affinity_names_follow_sqlite_order(self):
        self.assertEqual(affinity_of("INTEGER"), "INTEGER")
        self.assertEqual(affinity_of("BIGINT"), "INTEGER")
        self.assertEqual(affinity_of("VARCHAR(20)"), "TEXT")
        self.assertEqual(affinity_of("TEXT"), "TEXT")
        self.assertEqual(affinity_of("REAL"), "REAL")
        self.assertEqual(affinity_of("DOUBLE"), "REAL")
        self.assertEqual(affinity_of("NUMERIC"), "NUMERIC")
        self.assertEqual(affinity_of(""), "BLOB")
        self.assertEqual(affinity_of("BLOB"), "BLOB")


class TestAffinityApplication(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (a TEXT, b INTEGER, c REAL, d NUMERIC)")
        self.db.run("INSERT INTO t VALUES (5, '5', 5, '3.0')")

    def test_text_affinity_coerces_numbers_to_text(self):
        self.assertEqual(self.db.run("SELECT typeof(a), a FROM t")[1], [("text", "5")])

    def test_integer_affinity_coerces_numeric_strings(self):
        self.assertEqual(self.db.run("SELECT typeof(b), b FROM t")[1], [("integer", 5)])

    def test_real_affinity_keeps_real_type(self):
        self.assertEqual(self.db.run("SELECT typeof(c) FROM t")[1], [("real",)])

    def test_numeric_affinity_lossless_dot_zero_becomes_integer(self):
        self.assertEqual(self.db.run("SELECT typeof(d), d FROM t")[1], [("integer", 3)],
                         "'3.0' loses the dot and stores as integer")

    def test_real_affinity_coerces_numeric_strings(self):
        self.db.run("CREATE TABLE r (v REAL)")
        self.db.run("INSERT INTO r VALUES ('4'), ('x')")
        self.assertEqual(self.db.run("SELECT typeof(v) FROM r WHERE v = 4")[1], [("real",)])
        self.assertEqual(self.db.run("SELECT typeof(v) FROM r WHERE v = 'x'")[1], [("text",)])

    def test_non_numeric_string_stays_text(self):
        self.db.run("CREATE TABLE n (v INTEGER)")
        self.db.run("INSERT INTO n VALUES ('12abc')")
        self.assertEqual(self.db.run("SELECT typeof(v), v FROM n")[1], [("text", "12abc")])


class TestCollations(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (name TEXT COLLATE NOCASE)")
        self.db.run("INSERT INTO t VALUES ('Alpha'), ('beta')")

    def test_nocase_equality(self):
        self.assertEqual(self.db.run("SELECT name FROM t WHERE name = 'ALPHA'")[1], [("Alpha",)])

    def test_nocase_ordering(self):
        self.assertEqual([row[0] for row in self.db.run("SELECT name FROM t ORDER BY name")[1]],
                         ["Alpha", "beta"])

    def test_nocase_unique_conflict(self):
        self.db.run("CREATE TABLE u (name TEXT COLLATE NOCASE UNIQUE)")
        self.db.run("INSERT INTO u VALUES ('A')")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("INSERT INTO u VALUES ('a')")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: u.name")


class DifferentialM17(unittest.TestCase):
    CASES = [
        ["CREATE TABLE t (a TEXT, b INTEGER, c REAL, d NUMERIC)",
         "INSERT INTO t VALUES (5, '5', 5, '3.0')",
         "SELECT typeof(a), a, typeof(b), b, typeof(c), c, typeof(d), d FROM t"],
        ["CREATE TABLE t (v INTEGER)", "INSERT INTO t VALUES ('12abc')", "SELECT typeof(v), v FROM t"],
        ["CREATE TABLE t (v TEXT)", "INSERT INTO t VALUES (1.5)", "SELECT typeof(v), v FROM t"],
        ["CREATE TABLE t (v REAL)", "INSERT INTO t VALUES (3)", "SELECT typeof(v), v FROM t"],
        ["CREATE TABLE t (name TEXT COLLATE NOCASE)", "INSERT INTO t VALUES ('Alpha'),('beta')",
         "SELECT name FROM t WHERE name = 'ALPHA'"],
        ["CREATE TABLE t (name TEXT COLLATE NOCASE)", "INSERT INTO t VALUES ('Alpha'),('beta')",
         "SELECT name FROM t ORDER BY name"],
        ["CREATE TABLE u (name TEXT COLLATE NOCASE UNIQUE)", "INSERT INTO u VALUES ('A')",
         "INSERT INTO u VALUES ('a')"],
        ["CREATE TABLE u (name TEXT)", "INSERT INTO u VALUES ('A')", "INSERT INTO u VALUES ('a')",
         "SELECT count(*) FROM u"],
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
