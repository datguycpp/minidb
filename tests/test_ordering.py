"""Milestone 12 tests: ORDER BY, LIMIT/OFFSET, DISTINCT interaction."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402

SEED = [
    "CREATE TABLE t (v INTEGER, w INTEGER)",
    "INSERT INTO t VALUES (1,10),(2,20),(1,5),(NULL,40),(3,50)",
]


class TestOrdering(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SEED:
            self.db.run(statement)

    def test_nulls_first_ascending(self):
        rows = self.db.run("SELECT v FROM t ORDER BY v")[1]
        self.assertEqual(rows, [(None,), (1,), (1,), (2,), (3,)])

    def test_nulls_last_descending(self):
        self.assertEqual(self.db.run("SELECT v FROM t ORDER BY v DESC")[1],
                         [(3,), (2,), (1,), (1,), (None,)])

    def test_multi_key_mixed_directions(self):
        rows = self.db.run("SELECT v, w FROM t ORDER BY v ASC, w DESC")[1]
        self.assertEqual(rows, [(None, 40), (1, 10), (1, 5), (2, 20), (3, 50)])

    def test_order_by_expression(self):
        rows = self.db.run("SELECT v FROM t ORDER BY w * -1")[1]
        self.assertEqual(rows, [(3,), (None,), (2,), (1,), (1,)])

    def test_order_by_alias(self):
        rows = self.db.run("SELECT v AS x FROM t ORDER BY x")[1]
        self.assertEqual(rows, [(None,), (1,), (1,), (2,), (3,)])

    def test_group_by_then_order(self):
        rows = self.db.run("SELECT v, count(*) FROM t GROUP BY v ORDER BY v DESC")[1]
        self.assertEqual(rows, [(3, 1), (2, 1), (1, 2), (None, 1)])


class TestStarSafety(unittest.TestCase):
    """SELECT * puts a bare Star in node.items; the ORDER BY path must not assume every
    item is a SelectItem (this crashed four t2c cases with AttributeError)."""

    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (v INTEGER, w TEXT)")
        self.db.run("INSERT INTO t VALUES (2, 'b'), (1, 'a')")

    def test_star_with_order_by(self):
        self.assertEqual(self.db.run("SELECT * FROM t ORDER BY v")[1], [(1, "a"), (2, "b")])

    def test_star_with_order_by_desc(self):
        self.assertEqual(self.db.run("SELECT * FROM t ORDER BY v DESC")[1], [(2, "b"), (1, "a")])

    def test_star_with_order_and_limit(self):
        self.assertEqual(self.db.run("SELECT * FROM t ORDER BY v LIMIT 1")[1], [(1, "a")])

    def test_star_with_window_function(self):
        rows = self.db.run("SELECT *, ROW_NUMBER() OVER (ORDER BY v) AS rn "
                           "FROM t ORDER BY v")[1]
        self.assertEqual(rows, [(1, "a", 1), (2, "b", 2)])


class TestLimit(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE n (v INTEGER)")
        for value in (1, 2, 3, 4, 5):
            self.db.run(f"INSERT INTO n VALUES ({value})")

    def test_limit(self):
        self.assertEqual(self.db.run("SELECT v FROM n LIMIT 2")[1], [(1,), (2,)])

    def test_limit_offset_keyword(self):
        self.assertEqual(self.db.run("SELECT v FROM n LIMIT 2 OFFSET 3")[1], [(4,), (5,)])

    def test_limit_comma_form_is_offset_first(self):
        self.assertEqual(self.db.run("SELECT v FROM n LIMIT 2, 2")[1], [(3,), (4,)])

    def test_limit_zero(self):
        self.assertEqual(self.db.run("SELECT v FROM n LIMIT 0")[1], [])

    def test_limit_negative_is_unbounded(self):
        self.assertEqual(len(self.db.run("SELECT v FROM n LIMIT -1")[1]), 5)

    def test_limit_applies_after_order(self):
        self.assertEqual(self.db.run("SELECT v FROM n ORDER BY v DESC LIMIT 2")[1], [(5,), (4,)])


class DifferentialM12(unittest.TestCase):
    CASES = [
        SEED + ["SELECT v FROM t ORDER BY v"],
        SEED + ["SELECT v FROM t ORDER BY v DESC"],
        SEED + ["SELECT v, w FROM t ORDER BY v ASC, w DESC"],
        SEED + ["SELECT v FROM t ORDER BY w * -1"],
        SEED + ["SELECT v AS x FROM t ORDER BY x"],
        SEED + ["SELECT v, count(*) FROM t GROUP BY v ORDER BY v DESC"],
        SEED + ["SELECT DISTINCT v FROM t ORDER BY w"],
        SEED + ["SELECT v FROM t ORDER BY v LIMIT 2"],
        SEED + ["SELECT v FROM t LIMIT 2, 2"],
        SEED + ["SELECT v FROM t LIMIT 0"],
        SEED + ["SELECT v FROM t LIMIT -1"],
        SEED + ["SELECT v FROM t WHERE v IS NOT NULL ORDER BY v DESC LIMIT 1"],
        SEED + ["SELECT DISTINCT v FROM t ORDER BY v LIMIT 3"],
        SEED + ["SELECT v FROM t ORDER BY w DESC"],
        SEED + ["SELECT * FROM t ORDER BY v"],
        SEED + ["SELECT * FROM t ORDER BY w DESC, v"],
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
