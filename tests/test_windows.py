"""Milestone 15 tests: window functions, UPDATE/DELETE, and the recursive-CTE DAG regression."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402

SEED = [
    "CREATE TABLE t (g TEXT, v INTEGER)",
    "INSERT INTO t VALUES ('a', 1), ('a', 2), ('a', 2), ('b', 10), ('b', 20)",
]


class TestWindows(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SEED:
            self.db.run(statement)

    def test_row_number(self):
        self.assertEqual(self.db.run("SELECT v, ROW_NUMBER() OVER (ORDER BY v) AS rn "
                                     "FROM t ORDER BY v, rn")[1],
                         [(1, 1), (2, 2), (2, 3), (10, 4), (20, 5)])

    def test_rank_vs_dense_rank_on_ties(self):
        rows = self.db.run("SELECT v, RANK() OVER (ORDER BY v) AS r, "
                           "DENSE_RANK() OVER (ORDER BY v) AS d FROM t ORDER BY v, d")[1]
        self.assertEqual(rows, [(1, 1, 1), (2, 2, 2), (2, 2, 2), (10, 4, 3), (20, 5, 4)],
                         "RANK leaves a gap after a tie; DENSE_RANK does not")

    def test_running_sum_with_explicit_frame(self):
        rows = self.db.run("SELECT v, SUM(v) OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING "
                           "AND CURRENT ROW) AS s FROM t ORDER BY v, s")[1]
        self.assertEqual(rows, [(1, 1), (2, 3), (2, 5), (10, 15), (20, 35)])

    def test_partition_restarts_numbering(self):
        rows = self.db.run("SELECT g, v, ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) AS rn "
                           "FROM t ORDER BY g, v, rn")[1]
        self.assertEqual([row[2] for row in rows], [1, 2, 3, 1, 2])

    def test_partition_average(self):
        rows = self.db.run("SELECT g, AVG(v) OVER (PARTITION BY g) AS pa FROM t ORDER BY g")[1]
        self.assertEqual(rows, [("a", 5 / 3), ("a", 5 / 3), ("a", 5 / 3),
                                ("b", 15.0), ("b", 15.0)])

    def test_lag_with_default(self):
        rows = self.db.run("SELECT v, LAG(v, 1, 0) OVER (ORDER BY v) AS prev "
                           "FROM t ORDER BY v")[1]
        self.assertEqual(rows[0], (1, 0))
        self.assertEqual(rows[-1], (20, 10))


class TestUpdateDelete(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SEED:
            self.db.run(statement)

    def test_update_where(self):
        self.db.run("UPDATE t SET v = v + 1 WHERE g = 'a'")
        self.assertEqual(self.db.run("SELECT v FROM t WHERE g = 'a' ORDER BY v")[1],
                         [(2,), (3,), (3,)])

    def test_update_without_where(self):
        self.db.run("UPDATE t SET g = 'z'")
        self.assertEqual(self.db.run("SELECT count(*) FROM t WHERE g = 'z'")[1], [(5,)])

    def test_assignments_read_the_old_row(self):
        self.db.run("CREATE TABLE p (a INTEGER, b INTEGER)")
        self.db.run("INSERT INTO p VALUES (1, 2)")
        self.db.run("UPDATE p SET a = b, b = a")
        self.assertEqual(self.db.run("SELECT a, b FROM p")[1], [(2, 1)], "a swap, not a copy")

    def test_delete_where(self):
        self.db.run("DELETE FROM t WHERE v > 5")
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(3,)])

    def test_delete_without_where(self):
        self.db.run("DELETE FROM t")
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(0,)])


class TestRecursiveDag(unittest.TestCase):
    """A DAG (two parents reaching the same child) must not duplicate rows under UNION.
    The seed branch itself can contain duplicates — SELECT src, 0 over edge rows — so the
    dedupe has to cover the seed, not only the recursive step."""

    def test_dag_union_deduplicates_seed_and_recursion(self):
        db = Database()
        db.run("CREATE TABLE edge (src INTEGER, dst INTEGER)")
        db.run("INSERT INTO edge VALUES (1,2),(1,3),(2,4),(3,4)")
        rows = db.run("WITH RECURSIVE walk(id, depth) AS ("
                      "SELECT src, 0 FROM edge WHERE src = 1 UNION "
                      "SELECT edge.dst, walk.depth + 1 FROM edge JOIN walk ON edge.src = walk.id"
                      ") SELECT id, depth FROM walk ORDER BY id, depth")[1]
        self.assertEqual(rows, [(1, 0), (2, 1), (3, 1), (4, 2)])


class DifferentialM15(unittest.TestCase):
    CASES = [
        SEED + ["SELECT v, ROW_NUMBER() OVER (ORDER BY v) AS rn FROM t ORDER BY v"],
        SEED + ["SELECT v, RANK() OVER (ORDER BY v) AS r, DENSE_RANK() OVER (ORDER BY v) AS d "
                "FROM t ORDER BY v"],
        SEED + ["SELECT v, SUM(v) OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING AND "
                "CURRENT ROW) AS s FROM t ORDER BY v"],
        SEED + ["SELECT g, v, ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) AS rn "
                "FROM t ORDER BY g, v"],
        SEED + ["SELECT g, AVG(v) OVER (PARTITION BY g) AS pa FROM t ORDER BY g, v"],
        SEED + ["SELECT v, LAG(v, 1, 0) OVER (ORDER BY v) AS prev FROM t ORDER BY v"],
        SEED + ["UPDATE t SET v = v + 1 WHERE g = 'a'", "SELECT g, v FROM t ORDER BY g, v"],
        SEED + ["UPDATE t SET g = 'z'", "SELECT g, count(*) FROM t GROUP BY g ORDER BY g"],
        SEED + ["DELETE FROM t WHERE v > 5", "SELECT count(*) FROM t"],
        SEED + ["DELETE FROM t", "SELECT count(*) FROM t"],
        ["CREATE TABLE edge (src INTEGER, dst INTEGER)",
         "INSERT INTO edge VALUES (1,2),(1,3),(2,4),(3,4)",
         "WITH RECURSIVE walk(id, depth) AS (SELECT src, 0 FROM edge WHERE src = 1 UNION "
         "SELECT edge.dst, walk.depth + 1 FROM edge JOIN walk ON edge.src = walk.id) "
         "SELECT id, depth FROM walk ORDER BY id, depth"],
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
