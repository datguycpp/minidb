"""Correlated subqueries and EXISTS: the M8-remainder gap, pinned by t3c."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402

SEED = [
    "CREATE TABLE t (id INTEGER, g TEXT, v INTEGER)",
    "CREATE TABLE u (g TEXT, m INTEGER)",
    "INSERT INTO t VALUES (1, 'a', 10), (2, 'a', 20), (3, 'b', 5)",
    "INSERT INTO u VALUES ('a', 20), ('b', 5)",
]


class TestCorrelatedSubqueries(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SEED:
            self.db.run(statement)

    def test_correlated_scalar_in_projection(self):
        rows = self.db.run("SELECT id, (SELECT m FROM u WHERE u.g = t.g) FROM t ORDER BY id")[1]
        self.assertEqual(rows, [(1, 20), (2, 20), (3, 5)])

    def test_correlated_scalar_in_where(self):
        rows = self.db.run("SELECT id FROM t WHERE v = (SELECT MAX(m) FROM u WHERE u.g = t.g) "
                           "ORDER BY id")[1]
        self.assertEqual(rows, [(2,), (3,)])

    def test_correlated_exists(self):
        rows = self.db.run("SELECT id FROM t WHERE EXISTS "
                           "(SELECT 1 FROM u WHERE u.g = t.g) ORDER BY id")[1]
        self.assertEqual(rows, [(1,), (2,), (3,)])

    def test_correlated_not_exists(self):
        self.db.run("DELETE FROM u WHERE g = 'b'")
        rows = self.db.run("SELECT id FROM t WHERE NOT EXISTS "
                           "(SELECT 1 FROM u WHERE u.g = t.g) ORDER BY id")[1]
        self.assertEqual(rows, [(3,)])

    def test_uncorrelated_subquery_is_not_shadowed_by_the_outer_row(self):
        self.assertEqual(self.db.run("SELECT id FROM t WHERE v = (SELECT MAX(v) FROM t)")[1],
                         [(2,)], "the inner query must resolve its own column, not the outer row")


class DifferentialCorrelated(unittest.TestCase):
    CASES = [
        SEED + ["SELECT id, (SELECT m FROM u WHERE u.g = t.g) FROM t ORDER BY id"],
        SEED + ["SELECT id FROM t WHERE v = (SELECT MAX(m) FROM u WHERE u.g = t.g) ORDER BY id"],
        SEED + ["SELECT id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.g = t.g) ORDER BY id"],
        SEED + ["SELECT id FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.g = t.g) ORDER BY id"],
        SEED + ["SELECT id FROM t WHERE v = (SELECT MAX(v) FROM t)"],
        SEED + ["SELECT id FROM t WHERE v > (SELECT AVG(m) FROM u)"],
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
