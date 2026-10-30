"""Milestone 13 tests: UNION / UNION ALL / INTERSECT / EXCEPT."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402

SEED = [
    "CREATE TABLE t1 (v INTEGER)",
    "CREATE TABLE t2 (v INTEGER)",
    "INSERT INTO t1 VALUES (1), (2)",
    "INSERT INTO t2 VALUES (2), (3)",
]


class TestSetOperations(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SEED:
            self.db.run(statement)

    def test_union_deduplicates(self):
        self.assertEqual(self.db.run("SELECT v FROM t1 UNION SELECT v FROM t2 ORDER BY v")[1],
                         [(1,), (2,), (3,)])

    def test_union_all_keeps_multiplicity(self):
        self.assertEqual(self.db.run("SELECT v FROM t1 UNION ALL SELECT v FROM t2 ORDER BY v")[1],
                         [(1,), (2,), (2,), (3,)])

    def test_union_all_keeps_input_duplicates(self):
        self.db.run("INSERT INTO t1 VALUES (2)")
        rows = self.db.run("SELECT v FROM t1 UNION ALL SELECT v FROM t1 ORDER BY v")[1]
        self.assertEqual(rows, [(1,), (1,), (2,), (2,), (2,), (2,)])

    def test_intersect(self):
        self.assertEqual(self.db.run("SELECT v FROM t1 INTERSECT SELECT v FROM t2 ORDER BY v")[1],
                         [(2,)])

    def test_except(self):
        self.assertEqual(self.db.run("SELECT v FROM t1 EXCEPT SELECT v FROM t2 ORDER BY v")[1],
                         [(1,)])

    def test_null_rows_compare_equal_in_sets(self):
        self.db.run("CREATE TABLE n1 (v INTEGER)")
        self.db.run("CREATE TABLE n2 (v INTEGER)")
        self.db.run("INSERT INTO n1 VALUES (NULL), (1)")
        self.db.run("INSERT INTO n2 VALUES (NULL)")
        self.assertEqual(self.db.run("SELECT v FROM n1 INTERSECT SELECT v FROM n2")[1], [(None,)])
        self.assertEqual(self.db.run("SELECT v FROM n1 EXCEPT SELECT v FROM n2")[1], [(1,)])

    def test_column_names_come_from_first_branch(self):
        columns, _ = self.db.run("SELECT v AS val FROM t1 UNION SELECT v FROM t2")
        self.assertEqual(columns, ["val"])

    def test_compound_order_by_position(self):
        rows = self.db.run("SELECT v FROM t1 UNION SELECT v FROM t2 ORDER BY 1 DESC")[1]
        self.assertEqual(rows, [(3,), (2,), (1,)])

    def test_compound_limit(self):
        rows = self.db.run("SELECT v FROM t1 UNION SELECT v FROM t2 ORDER BY 1 LIMIT 2")[1]
        self.assertEqual(rows, [(1,), (2,)])


class TestCompoundPrecedence(unittest.TestCase):
    """SQLite is not the SQL standard: all compound operators share one precedence level and
    associate left. Verified against the pinned binary — a UNION-then-INTERSECT chain returns
    the INTERSECT-first reading's complement."""

    def setUp(self):
        self.db = Database()
        for statement in ["CREATE TABLE a (v INTEGER)", "CREATE TABLE b (v INTEGER)",
                          "CREATE TABLE c (v INTEGER)"]:
            self.db.run(statement)
        self.db.run("INSERT INTO a VALUES (1), (2)")
        self.db.run("INSERT INTO b VALUES (2), (3)")
        self.db.run("INSERT INTO c VALUES (2)")

    def test_intersect_does_not_bind_tighter(self):
        # (a UNION b) INTERSECT c = {2}; a UNION (b INTERSECT c) would be {1, 2}.
        rows = self.db.run("SELECT v FROM a UNION SELECT v FROM b INTERSECT SELECT v FROM c "
                           "ORDER BY v")[1]
        self.assertEqual(rows, [(2,)], "flat left-associative, per the reference")

    def test_except_chain_is_left_associative(self):
        self.assertEqual(self.db.run("SELECT v FROM a EXCEPT SELECT v FROM b EXCEPT "
                                     "SELECT v FROM c ORDER BY v")[1], [(1,)])


class TestCompoundErrors(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_column_count_mismatch(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("SELECT 1 UNION SELECT 1, 2")
        self.assertEqual(str(ctx.exception),
                         "SELECTs to the left and right of UNION do not have the same number "
                         "of result columns")

    def test_order_by_outside_result_set(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("SELECT 1 AS a UNION SELECT 2 ORDER BY b")
        self.assertEqual(str(ctx.exception),
                         "1st ORDER BY term does not match any column in the result set")

    def test_order_by_position_out_of_range(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("SELECT 1 AS a UNION SELECT 2 ORDER BY 3")
        self.assertEqual(str(ctx.exception),
                         "3rd ORDER BY term out of range - should be between 1 and 1")


class DifferentialM13(unittest.TestCase):
    CASES = [
        SEED + ["SELECT v FROM t1 UNION SELECT v FROM t2 ORDER BY v"],
        SEED + ["SELECT v FROM t1 UNION ALL SELECT v FROM t2 ORDER BY v"],
        SEED + ["SELECT v FROM t1 INTERSECT SELECT v FROM t2 ORDER BY v"],
        SEED + ["SELECT v FROM t1 EXCEPT SELECT v FROM t2 ORDER BY v"],
        SEED + ["SELECT v FROM t1 UNION SELECT v FROM t2 ORDER BY 1 DESC"],
        SEED + ["SELECT v FROM t1 UNION SELECT v FROM t2 ORDER BY 1 LIMIT 2"],
        SEED + ["SELECT v AS val FROM t1 UNION SELECT v FROM t2 ORDER BY val"],
        SEED + ["SELECT v FROM t1 UNION SELECT v FROM t2 INTERSECT SELECT v FROM t2 ORDER BY v"],
        SEED + ["SELECT * FROM (SELECT v FROM t1 UNION SELECT v FROM t2) AS u "
                "WHERE u.v > 1 ORDER BY u.v"],
        SEED + ["SELECT v FROM t1 WHERE v IN (SELECT v FROM t2 UNION SELECT 9) ORDER BY v"],
        SEED + ["SELECT v FROM t1 UNION SELECT v FROM t2 EXCEPT SELECT v FROM t2 ORDER BY v"],
        SEED + ["SELECT 1 AS a UNION SELECT 1"],
        SEED + ["SELECT 1 AS a UNION ALL SELECT 1"],
        SEED + ["SELECT 1 AS a EXCEPT SELECT 1"],
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
