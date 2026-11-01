"""Milestone 38 parity tests: the C aggregate path must be indistinguishable from Python.

The gate is mechanical — same answers, same errors, same corpus — so the test does the only
comparison that proves it: run each shape with the accelerator on and with it off (the Python
path is the reference) and require identical rows *or* identical error text. Shapes outside the
C path's declared scope fall back, and the test asserts that too, because "it fell back" is the
reason the corpus can't see the difference.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minidb.engine as engine  # noqa: E402
from minidb.engine import Database  # noqa: E402

NATIVE = engine._native is not None


def build(rows):
    db = Database()
    db.run("CREATE TABLE t (a INTEGER, v INTEGER, w REAL, s TEXT, b BLOB)")
    for row in rows:
        values = tuple(row) + (None,) * (5 - len(row))
        db.run("INSERT INTO t VALUES (%s)" % ", ".join(_literal(value) for value in values))
    return db


def _literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return repr(value)


def run_off(sql, rows):
    saved = engine._native
    engine._native = None
    try:
        db = build(rows)
        try:
            return ("ok", db.run(sql)[1])
        except Exception as exc:                      # noqa: BLE001 - the error is the artifact
            return ("error", type(exc).__name__, str(exc))
        finally:
            db.close()
    finally:
        engine._native = saved


def run_on(sql, rows):
    db = build(rows)
    try:
        return ("ok", db.run(sql)[1]), db.last_plan
    except Exception as exc:                          # noqa: BLE001
        return ("error", type(exc).__name__, str(exc)), None
    finally:
        db.close()


ROWS = [
    (1, 10, 1.5, "alpha", b"\x01"),
    (1, None, 2.5, "beta", b"\x02"),
    (2, -3, None, "alpha", None),
    (2, 7, 3.25, "gamma", b"\x03"),
    (None, 0, 0.0, None, b""),
    (3, 5, -1.0, "alpha", b"\x04"),
]


class TestParity(unittest.TestCase):
    def setUp(self):
        if not NATIVE:
            self.skipTest("C accelerator not built; the Python path is the reference")

    def check(self, sql):
        reference = run_off(sql, ROWS)
        produced = run_on(sql, ROWS)[0]
        self.assertEqual(produced, reference, f"diverged: {sql}")

    def test_simple_aggregates(self):
        for sql in [
            "SELECT COUNT(*) FROM t",
            "SELECT COUNT(v) FROM t",
            "SELECT SUM(v) FROM t",
            "SELECT AVG(v) FROM t",
            "SELECT MIN(v), MAX(v) FROM t",
            "SELECT COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM t",
            "SELECT SUM(w), AVG(w) FROM t",
            "SELECT MIN(s), MAX(s) FROM t",
            "SELECT COUNT(b), MIN(b), MAX(b) FROM t",
        ]:
            self.check(sql)

    def test_grouped_aggregates(self):
        for sql in [
            "SELECT a, COUNT(*), SUM(v) FROM t GROUP BY a",
            "SELECT s, COUNT(*), AVG(v) FROM t GROUP BY s",
            "SELECT a, MIN(s), MAX(s) FROM t GROUP BY a",
            "SELECT a, COUNT(*) FROM t GROUP BY a LIMIT 2",
            "SELECT a, COUNT(*) AS n FROM t GROUP BY a",
        ]:
            self.check(sql)

    def test_empty_table(self):
        self.assertEqual(run_off("SELECT COUNT(*), SUM(v), AVG(v) FROM t", []),
                         run_on("SELECT COUNT(*), SUM(v), AVG(v) FROM t", [])[0])
        self.assertEqual(run_off("SELECT a, COUNT(*) FROM t GROUP BY a", []),
                         run_on("SELECT a, COUNT(*) FROM t GROUP BY a", [])[0])

    def test_all_null_column(self):
        rows = [(1, None, None, None, None), (2, None, None, None, None)]
        for sql in ("SELECT COUNT(v), SUM(v), AVG(v), MIN(v) FROM t",
                    "SELECT COUNT(*), SUM(v) FROM t"):
            self.assertEqual(run_off(sql, rows), run_on(sql, rows)[0])

    def test_text_under_sum_is_the_same_error(self):
        for sql in ("SELECT SUM(s) FROM t", "SELECT AVG(s) FROM t"):
            reference = run_off(sql, ROWS)
            produced = run_on(sql, ROWS)[0]
            self.assertEqual(produced[0], "error")
            self.assertEqual(produced, reference, "the fallback must raise what Python raises")

    def test_unsupported_shapes_fall_back(self):
        cases = [
            "SELECT COUNT(*) FROM t WHERE a > 0",          # WHERE
            "SELECT SUM(v) * 2 FROM t",                     # expression
            "SELECT SUM(v + 1) FROM t",                     # expression argument
            "SELECT COUNT(DISTINCT a) FROM t",              # DISTINCT
            "SELECT a, COUNT(*) FROM t GROUP BY a ORDER BY a",   # ORDER BY
        ]
        for sql in cases:
            self.check(sql)
            _, plan = run_on(sql, ROWS)
            if plan is not None:
                self.assertFalse(plan.get("native"), f"should have fallen back: {sql}")

    def test_the_fast_path_actually_runs_where_declared(self):
        for sql in ("SELECT COUNT(*), SUM(v) FROM t",
                    "SELECT a, COUNT(*), SUM(v) FROM t GROUP BY a"):
            _, plan = run_on(sql, ROWS)
            self.assertTrue(plan and plan.get("native"), f"expected the C path: {sql}")

    def test_float_promotion_matches_python_sum(self):
        rows = [(1, 1), (2, 2.5), (3, 3)]
        self.assertEqual(run_off("SELECT SUM(v), AVG(v) FROM t", rows),
                         run_on("SELECT SUM(v), AVG(v) FROM t", rows)[0])
        self.assertEqual(run_on("SELECT SUM(v) FROM t", rows)[0][1], [(6.5,)])


if __name__ == "__main__":
    unittest.main()
