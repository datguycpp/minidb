"""Milestone 11 tests: built-in functions.

Same shape as M10 — the differential table is the real test, because "abs returns the absolute
value" is not the interesting question; "what does SQLite do with abs('x')" is.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402


class DifferentialFunctions(unittest.TestCase):
    CASES = [
        ["SELECT abs(-3), abs(-3.5), abs('5'), abs('-5')"],
        ["SELECT length('abc'), length(123), length('héllo'), length(NULL)"],
        ["SELECT substr('abcdef', 2, 3), substr('abcdef', -3), substr('abcdef', 2, 99)"],
        ["SELECT upper('aBc'), lower('aBc'), upper('héllo')"],
        ["SELECT trim('  x  '), trim('xxaxx', 'x'), ltrim('xxax', 'x'), rtrim('xaxx', 'x')"],
        ["SELECT replace('abcabc', 'b', 'X'), replace('abc', '', 'X')"],
        ["SELECT round(2.567, 2), round(2.5), round(-2.5), round(2.4), round(2.567)"],
        ["SELECT typeof(1), typeof(1.0), typeof('x'), typeof(NULL)"],
        ["SELECT coalesce(NULL, NULL, 3), ifnull(NULL, 'z'), ifnull('y', 'z')"],
        ["SELECT nullif(1, 1), nullif(1, 2), nullif(NULL, 1)"],
        ["SELECT min(3, 1, 2), max(3, 1, 2), min('a', 'b'), max('a', 'b')"],
        ["SELECT instr('abcabc', 'cab'), instr('abc', 'z')"],
        ["SELECT date('2020-01-02 03:04:05'), time('2020-01-02 03:04:05'), datetime('2020-01-02')"],
        ["CREATE TABLE t (a TEXT, b INTEGER)", "INSERT INTO t VALUES ('hello', -4)",
         "SELECT upper(a), abs(b), length(a) FROM t"],
        ["SELECT substr('abcdef', 0, 3), substr('abcdef', 1, 0)"],
    ]

    def test_cases_match_sqlite(self):
        for statements in self.CASES:
            with self.subTest(sql=statements[-1]):
                expected = bc.run_sqlite([statements])
                actual = bc.run_minidb([statements])
                ok, detail = bc.compare(expected, actual)
                self.assertTrue(ok, f"{statements[-1]} -> {detail}")


class TestFunctionDetails(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_round_half_away_from_zero(self):
        self.assertEqual(self.db.run("SELECT round(2.5), round(-2.5)")[1], [(3, -3)],
                         "Python's round() is half-to-even; SQLite's is half-away-from-zero")

    def test_length_counts_characters_not_bytes(self):
        self.assertEqual(self.db.run("SELECT length('héllo')")[1], [(5,)])

    def test_null_propagation_for_string_functions(self):
        self.assertEqual(self.db.run("SELECT upper(NULL), length(NULL)")[1], [(None, None)])

    def test_coalesce_is_null_tolerant(self):
        self.assertEqual(self.db.run("SELECT coalesce(NULL, NULL, 3)")[1], [(3,)])

    def test_nullif_returns_null_on_equal(self):
        self.assertEqual(self.db.run("SELECT nullif(1, 1), nullif(1, 2)")[1], [(None, 1)])

    def test_aggregate_and_scalar_min_coexist(self):
        self.db.run("CREATE TABLE t (a INTEGER)")
        self.db.run("INSERT INTO t VALUES (5), (2), (9)")
        self.assertEqual(self.db.run("SELECT min(a), max(a) FROM t")[1], [(2, 9)], "one argument is an aggregate")
        self.assertEqual(sorted(self.db.run("SELECT min(a, 3) FROM t")[1]), [(2,), (3,), (3,)],
                         "two arguments are scalar, evaluated per row")

    def test_unknown_function_reports_sqlite_wording(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("SELECT nosuchfunc(1)")
        self.assertEqual(str(ctx.exception), "no such function: nosuchfunc")

    def test_substr_negative_start(self):
        self.assertEqual(self.db.run("SELECT substr('abcdef', -3)")[1], [("def",)])


if __name__ == "__main__":
    unittest.main()
