"""Milestone 8 tests: joins, aggregates, subqueries.

The differential half runs the same statements through SQLite and MiniDB and requires identical
outcomes — including the awkward ones (COUNT on an empty table, NULL groups, scalar subquery
returning nothing).
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402


def seed(db):
    db.run("CREATE TABLE t (id INTEGER, a INTEGER)")
    db.run("CREATE TABLE u (id INTEGER, b TEXT)")
    for i in (1, 2, 3):
        db.run(f"INSERT INTO t VALUES ({i}, {i * 10})")
    db.run("INSERT INTO u VALUES (1, 'x')")
    db.run("INSERT INTO u VALUES (2, 'y')")
    db.run("INSERT INTO u VALUES (9, 'z')")


class TestJoins(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        seed(self.db)

    def test_inner_join(self):
        rows = self.db.run("SELECT t.id, u.b FROM t JOIN u ON t.id = u.id")[1]
        self.assertEqual(rows, [(1, "x"), (2, "y")])

    def test_join_with_alias(self):
        rows = self.db.run("SELECT x.id, y.b FROM t AS x JOIN u AS y ON x.id = y.id")[1]
        self.assertEqual(rows, [(1, "x"), (2, "y")])

    def test_join_plus_where(self):
        rows = self.db.run("SELECT t.id FROM t JOIN u ON t.id = u.id WHERE u.b = 'y'")[1]
        self.assertEqual(rows, [(2,)])

    def test_join_plan_is_reported(self):
        self.db.run("SELECT t.id FROM t JOIN u ON t.id = u.id")
        plan = self.db.last_plan
        self.assertEqual(plan["access"], "nested-loop")
        self.assertEqual(plan["tables"], ["t", "u"])
        self.assertEqual(plan["rows_examined"], 9, "3 outer rows x 3 inner rows")

    def test_unqualified_column_is_ambiguous(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("SELECT id FROM t JOIN u ON t.id = u.id")
        self.assertIn("no such column: id", str(ctx.exception))

    def test_unknown_qualified_column(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("SELECT t.nope FROM t JOIN u ON t.id = u.id")
        self.assertEqual(str(ctx.exception), "no such column: t.nope")


class TestAggregates(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        seed(self.db)

    def test_count_star(self):
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(3,)])

    def test_sum_min_max_avg(self):
        self.assertEqual(self.db.run("SELECT sum(a), min(a), max(a) FROM t")[1], [(60, 10, 30)])
        self.assertEqual(self.db.run("SELECT avg(a) FROM t")[1], [(20.0,)])

    def test_count_ignores_nulls(self):
        self.db.run("INSERT INTO t VALUES (4, NULL)")
        self.assertEqual(self.db.run("SELECT count(a) FROM t")[1], [(3,)])
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(4,)])

    def test_aggregate_over_empty_table(self):
        self.db.run("CREATE TABLE empty (x INTEGER)")
        self.assertEqual(self.db.run("SELECT count(*) FROM empty")[1], [(0,)])
        self.assertEqual(self.db.run("SELECT sum(x) FROM empty")[1], [(None,)])

    def test_group_by(self):
        rows = self.db.run("SELECT a, count(*) FROM t GROUP BY a")[1]
        self.assertEqual(sorted(rows), [(10, 1), (20, 1), (30, 1)])

    def test_group_by_null_groups_together(self):
        self.db.run("INSERT INTO t VALUES (4, NULL)")
        self.db.run("INSERT INTO t VALUES (5, NULL)")
        rows = self.db.run("SELECT a, count(*) FROM t GROUP BY a")[1]
        self.assertIn((None, 2), rows)

    def test_having(self):
        self.db.run("INSERT INTO t VALUES (6, 10)")
        rows = self.db.run("SELECT a FROM t GROUP BY a HAVING count(*) > 1")[1]
        self.assertEqual(rows, [(10,)])


class TestSubqueries(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        seed(self.db)

    def test_in_subquery(self):
        # t has ids 1,2,3 with a = 10,20,30, so a > 10 keeps ids 2 and 3; u offers 1,2,9.
        rows = self.db.run("SELECT b FROM u WHERE id IN (SELECT id FROM t WHERE a > 10)")[1]
        self.assertEqual(rows, [("y",)])

    def test_in_subquery_no_match(self):
        self.assertEqual(self.db.run("SELECT b FROM u WHERE id IN (SELECT id FROM t WHERE a > 99)")[1], [])

    def test_scalar_subquery(self):
        # FROM-less SELECT is milestone 10; the scalar subquery is exercised through WHERE here.
        self.assertEqual(self.db.run("SELECT id FROM t WHERE a = (SELECT max(a) FROM t)")[1], [(3,)])

    def test_scalar_subquery_empty_is_null(self):
        # No row matches a > 99, so the comparison against NULL keeps nothing.
        self.assertEqual(self.db.run("SELECT id FROM t WHERE a = (SELECT max(a) FROM t WHERE a > 99)")[1], [])

    def test_scalar_subquery_does_not_leak_plan(self):
        self.db.run("SELECT b FROM u WHERE id = (SELECT max(id) FROM u)")
        self.assertEqual(self.db.last_plan["table"], "u")


class TestInPredicates(unittest.TestCase):
    """NOT IN and qualified columns on a single table: both surfaced as gaps after M8's review."""

    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (a INTEGER)")
        self.db.run("INSERT INTO t VALUES (1), (2), (3), (NULL)")
        self.db.run("CREATE TABLE with_null (b INTEGER)")
        self.db.run("INSERT INTO with_null VALUES (1), (NULL)")
        self.db.run("CREATE TABLE clean (b INTEGER)")
        self.db.run("INSERT INTO clean VALUES (1)")

    def test_not_in_subquery(self):
        rows = self.db.run("SELECT a FROM t WHERE a NOT IN (SELECT b FROM clean)")[1]
        self.assertEqual(sorted(rows), [(2,), (3,)])

    def test_not_in_null_trap_returns_nothing(self):
        # A NULL in the subquery result makes NOT IN unknown for every row — SQLite returns none.
        self.assertEqual(self.db.run("SELECT a FROM t WHERE a NOT IN (SELECT b FROM with_null)")[1], [])

    def test_in_with_null_in_subquery(self):
        self.assertEqual(self.db.run("SELECT a FROM t WHERE a IN (SELECT b FROM with_null)")[1], [(1,)])

    def test_qualified_column_on_single_table(self):
        self.assertEqual(self.db.run("SELECT t.a FROM t WHERE t.a = 2")[1], [(2,)])


class TestJoinForms(unittest.TestCase):
    """LEFT/RIGHT/FULL/comma joins and derived tables — the t1c scope that M10 closed."""

    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE a (id INTEGER, x TEXT)")
        self.db.run("CREATE TABLE b (id INTEGER, y TEXT)")
        self.db.run("INSERT INTO a VALUES (1, 'a1'), (2, 'a2'), (3, 'a3')")
        self.db.run("INSERT INTO b VALUES (2, 'b2'), (3, 'b3'), (4, 'b4')")

    def test_comma_join(self):
        rows = self.db.run("SELECT a.id, b.id FROM a, b WHERE a.id = b.id")[1]
        self.assertEqual(rows, [(2, 2), (3, 3)])

    def test_left_join_pads_missing_right(self):
        rows = self.db.run("SELECT a.id, b.y FROM a LEFT JOIN b ON a.id = b.id")[1]
        self.assertEqual(rows, [(1, None), (2, "b2"), (3, "b3")])

    def test_right_join_pads_missing_left(self):
        rows = self.db.run("SELECT a.id, b.id FROM a RIGHT JOIN b ON a.id = b.id")[1]
        self.assertEqual(rows, [(2, 2), (3, 3), (None, 4)])

    def test_full_outer_join_pads_both_sides(self):
        rows = self.db.run("SELECT a.id, b.id FROM a FULL OUTER JOIN b ON a.id = b.id")[1]
        self.assertEqual(rows, [(1, None), (2, 2), (3, 3), (None, 4)])

    def test_derived_table(self):
        rows = self.db.run("SELECT x.id FROM (SELECT id FROM a WHERE id > 1) AS x")[1]
        self.assertEqual(rows, [(2,), (3,)])

    def test_from_less_select(self):
        self.assertEqual(self.db.run("SELECT 1, 2")[1], [(1, 2)])

    def test_from_less_aggregate(self):
        self.assertEqual(self.db.run("SELECT count(*)")[1], [(1,)])

    def test_scalar_subquery_with_alias(self):
        self.assertEqual(self.db.run("SELECT (SELECT max(id) FROM a) AS top")[1], [(3,)])


class DifferentialM8(unittest.TestCase):
    """Same statements, both engines, identical outcome."""

    CASES = [
        ["CREATE TABLE t (a INTEGER)", "CREATE TABLE u (b INTEGER)",
         "INSERT INTO t VALUES (1), (2), (3)", "INSERT INTO u VALUES (2), (4)",
         "SELECT t.a, u.b FROM t JOIN u ON t.a = u.b"],
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1), (1), (2), (NULL)",
         "SELECT a, count(*) FROM t GROUP BY a"],
        ["CREATE TABLE t (a INTEGER)", "SELECT count(*), sum(a), avg(a), min(a), max(a) FROM t"],
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (5), (7)",
         "SELECT a FROM t WHERE a > (SELECT min(a) FROM t)"],
        ["CREATE TABLE t (a INTEGER)", "CREATE TABLE u (b INTEGER)",
         "INSERT INTO u VALUES (1), (2), (3)",
         "SELECT b FROM u WHERE b IN (SELECT a FROM t)"],
        ["CREATE TABLE t (a INTEGER)", "CREATE TABLE u (b INTEGER)",
         "INSERT INTO t VALUES (1), (2)", "INSERT INTO u VALUES (2), (3)",
         "SELECT t.a, u.b FROM t JOIN u ON t.a = u.b WHERE u.b > 1"],
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1), (2), (3)",
         "SELECT a, count(*) FROM t GROUP BY a HAVING count(*) >= 1"],
        ["CREATE TABLE t (a INTEGER)", "CREATE TABLE u (b INTEGER)",
         "INSERT INTO t VALUES (1), (2), (NULL)", "INSERT INTO u VALUES (1), (NULL)",
         "SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)"],
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1), (2)",
         "SELECT t.a FROM t WHERE t.a = 2"],
        ["CREATE TABLE a (id INTEGER)", "CREATE TABLE b (id INTEGER)",
         "INSERT INTO a VALUES (1), (2), (3)", "INSERT INTO b VALUES (2), (3), (4)",
         "SELECT a.id, b.id FROM a LEFT JOIN b ON a.id = b.id"],
        ["CREATE TABLE a (id INTEGER)", "CREATE TABLE b (id INTEGER)",
         "INSERT INTO a VALUES (1), (2)", "INSERT INTO b VALUES (2), (9)",
         "SELECT a.id, b.id FROM a FULL OUTER JOIN b ON a.id = b.id"],
        ["CREATE TABLE a (id INTEGER)", "INSERT INTO a VALUES (1), (2), (3)",
         "SELECT x.id FROM (SELECT id FROM a WHERE id < 3) AS x"],
        ["SELECT 7, 'x'"],
        ["CREATE TABLE a (id INTEGER, x INTEGER)", "CREATE TABLE b (id INTEGER, y INTEGER)",
         "INSERT INTO a VALUES (1, 10), (2, 20)", "INSERT INTO b VALUES (1, 100), (3, 300)",
         "SELECT a.id, b.id FROM a, b WHERE a.id = b.id"],
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
