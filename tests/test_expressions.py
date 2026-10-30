"""Milestone 10 tests: the expression engine.

Every case runs through both engines — expression semantics are exactly where "obviously right"
and "SQLite" disagree, so the differential table is the test and the unit assertions only pin
the ones worth naming.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402


class DifferentialExpressions(unittest.TestCase):
    CASES = [
        # precedence and arithmetic
        ["SELECT 1 + 2 * 3", "SELECT (1 + 2) * 3", "SELECT 10 - 4 - 3"],
        # C-semantics division/modulo: sign follows the dividend
        ["SELECT -7 / 2, -7 % 2, 7 / -2, 7 % -2, 5 / 2, 5.0 / 2"],
        # division by zero is NULL, not an exception
        ["SELECT 1 / 0, 1 % 0, 1.0 / 0"],
        # numeric coercion of text
        ["SELECT '5' + 1, 'abc' + 1, '  12' + 0, 2 + '3.5'"],
        # concatenation
        ["SELECT 1 || 'x', 2.0 || 'x', 'a' || 'b', 'a' || NULL"],
        # CASE: searched, simple, missing ELSE
        ["SELECT CASE WHEN 1 THEN 'y' ELSE 'n' END",
         "SELECT CASE 2 WHEN 1 THEN 'a' WHEN 2 THEN 'b' ELSE 'c' END",
         "SELECT CASE WHEN 0 THEN 'y' END"],
        # CAST, including SQLite's forgiving string-to-number
        ["SELECT CAST('abc' AS INTEGER), CAST('12abc' AS INTEGER), CAST(3.9 AS INTEGER)",
         "SELECT CAST('3.5' AS REAL), CAST(1 AS TEXT), CAST(2.0 AS TEXT)"],
        # three-valued logic
        ["SELECT NULL AND 0, NULL AND 1, NULL OR 1, NULL OR 0, NOT NULL, NOT 0, NOT 1"],
        # IS / IS NOT are NULL-safe and never NULL
        ["SELECT 1 IS 1, 1 IS 2, NULL IS NULL, NULL IS NOT NULL, 1 IS NOT NULL"],
        # BETWEEN, including NOT BETWEEN and NULL operands
        ["SELECT 3 BETWEEN 1 AND 4, 3 NOT BETWEEN 4 AND 5, NULL BETWEEN 1 AND 2"],
        # arithmetic in WHERE
        ["CREATE TABLE t (a INTEGER, b INTEGER)",
         "INSERT INTO t VALUES (3, 2), (5, 5)",
         "SELECT a FROM t WHERE a - b > 0"],
        # unary minus on a column
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (3), (-4)",
         "SELECT -a FROM t"],
        # COUNT(DISTINCT ...)
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1), (1), (2), (NULL)",
         "SELECT count(DISTINCT a), count(a), count(*) FROM t"],
    ]

    def test_cases_match_sqlite(self):
        for statements in self.CASES:
            with self.subTest(sql=statements[-1]):
                expected = bc.run_sqlite([statements])
                actual = bc.run_minidb([statements])
                ok, detail = bc.compare(expected, actual)
                self.assertTrue(ok, f"{statements[-1]} -> {detail}")


class TestNamedBehaviours(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_division_truncates_toward_zero(self):
        self.assertEqual(self.db.run("SELECT -7 / 2")[1], [(-3,)], "SQLite truncates, it does not floor")

    def test_modulo_sign_follows_dividend(self):
        self.assertEqual(self.db.run("SELECT -7 % 2, 7 % -2")[1], [(-1, 1)])

    def test_division_by_zero_is_null(self):
        self.assertEqual(self.db.run("SELECT 1 / 0")[1], [(None,)])

    def test_cast_from_garbage_is_zero(self):
        self.assertEqual(self.db.run("SELECT CAST('abc' AS INTEGER)")[1], [(0,)])

    def test_case_without_else_is_null(self):
        self.assertEqual(self.db.run("SELECT CASE WHEN 0 THEN 'y' END")[1], [(None,)])

    def test_null_and_zero_is_false(self):
        self.assertEqual(self.db.run("SELECT NULL AND 0")[1], [(False,)])

    def test_null_or_one_is_true(self):
        self.assertEqual(self.db.run("SELECT NULL OR 1")[1], [(True,)])

    def test_is_never_returns_null(self):
        self.assertEqual(self.db.run("SELECT NULL IS NULL, NULL IS 1")[1], [(True, False)])

    def test_where_numeric_truthiness(self):
        self.db.run("CREATE TABLE t (a INTEGER)")
        self.db.run("INSERT INTO t VALUES (0), (1), (2)")
        self.assertEqual(self.db.run("SELECT a FROM t WHERE a")[1], [(1,), (2,)])

    def test_concatenation_of_integral_float_keeps_point_zero(self):
        self.assertEqual(self.db.run("SELECT 2.0 || 'x'")[1], [("2.0x",)])

    def test_count_distinct_ignores_nulls(self):
        self.db.run("CREATE TABLE t (a INTEGER)")
        self.db.run("INSERT INTO t VALUES (1), (1), (2), (NULL)")
        self.assertEqual(self.db.run("SELECT count(DISTINCT a) FROM t")[1], [(2,)])


if __name__ == "__main__":
    unittest.main()
