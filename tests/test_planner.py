"""Milestone 5 tests: the planner has to make a *choice*, so these tests assert which access
path was chosen and why — not just that the rows came back."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.engine import Database


class PlannerBase(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (a INTEGER, b INTEGER, c TEXT)")
        # a = 1 for most rows (unselective), b = 7 for exactly one row.
        for i in range(1, 101):
            b = 7 if i == 42 else 1000 + i
            self.db.run(f"INSERT INTO t VALUES (1, {b}, 'row-{i}')")

    def plan(self, sql):
        self.db.run(sql)
        return self.db.last_plan


class TestChoiceRule(PlannerBase):
    def test_picks_smallest_estimate_among_two_indexes(self):
        self.db.run("CREATE INDEX idx_a ON t (a)")
        self.db.run("CREATE INDEX idx_b ON t (b)")
        plan = self.plan("SELECT c FROM t WHERE a = 1 AND b = 7")
        self.assertEqual(plan["access"], "index")
        self.assertEqual(plan["index"], "idx_b", "the 1-row bucket must beat the 100-row bucket")
        self.assertEqual(plan["rows_examined"], 1)
        self.assertEqual(plan["reason"], "smallest-estimate")

    def test_reports_every_candidate_with_its_estimate(self):
        self.db.run("CREATE INDEX idx_a ON t (a)")
        self.db.run("CREATE INDEX idx_b ON t (b)")
        plan = self.plan("SELECT c FROM t WHERE a = 1 AND b = 7")
        estimates = {c["name"]: c["estimated_rows"] for c in plan["candidates"]}
        self.assertEqual(estimates, {"idx_a": 100, "idx_b": 1})

    def test_unselective_index_loses_to_a_scan(self):
        self.db.run("CREATE INDEX idx_a ON t (a)")
        plan = self.plan("SELECT c FROM t WHERE a = 1")
        self.assertEqual(plan["access"], "full")
        self.assertEqual(plan["reason"], "index-not-selective")
        self.assertEqual(plan["rows_examined"], 100)

    def test_selective_range_uses_the_index(self):
        db = Database()
        db.run("CREATE TABLE t (v INTEGER)")
        for start in range(1, 1001, 100):
            db.run("INSERT INTO t VALUES " + ", ".join(f"({i % 100})" for i in range(start, start + 100)))
        db.run("CREATE INDEX idx_v ON t (v)")
        db.run("SELECT * FROM t WHERE v = 99")
        self.assertEqual(db.last_plan["access"], "index", "equality is unchanged")

    def test_wide_range_falls_back_to_a_scan_by_cost(self):
        db = Database()
        db.run("CREATE TABLE t (v INTEGER)")
        for start in range(1, 1001, 100):
            db.run("INSERT INTO t VALUES " + ", ".join(f"({i % 100})" for i in range(start, start + 100)))
        db.run("CREATE INDEX idx_v ON t (v)")
        db.run("SELECT * FROM t WHERE v > 10")
        self.assertEqual(db.last_plan["access"], "full")
        self.assertEqual(db.last_plan["reason"], "index-not-selective",
                         "a range touching most of the table costs more than a scan")

    def test_narrow_range_uses_the_index(self):
        db = Database()
        db.run("CREATE TABLE t (v INTEGER)")
        for start in range(1, 1001, 100):
            db.run("INSERT INTO t VALUES " + ", ".join(f"({i % 100})" for i in range(start, start + 100)))
        db.run("CREATE INDEX idx_v ON t (v)")
        db.run("SELECT * FROM t WHERE v > 98")
        self.assertEqual(db.last_plan["access"], "index")

    def test_no_index_means_scan(self):
        plan = self.plan("SELECT c FROM t WHERE b = 7")
        self.assertEqual(plan["access"], "full")
        self.assertEqual(plan["reason"], "no-eligible-index")
        self.assertEqual(plan["candidates"], [])

    def test_no_where_means_scan(self):
        self.db.run("CREATE INDEX idx_a ON t (a)")
        plan = self.plan("SELECT c FROM t")
        self.assertEqual(plan["access"], "full")
        self.assertEqual(plan["reason"], "no-predicate")


class TestTieBreaks(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (a INTEGER, b INTEGER)")
        self.db.run("INSERT INTO t VALUES (1, 2), (3, 4)")

    def test_more_columns_wins_a_tie(self):
        self.db.run("CREATE INDEX idx_a ON t (a)")
        self.db.run("CREATE INDEX idx_ab ON t (a, b)")
        plan = self.db.run("SELECT * FROM t WHERE a = 1 AND b = 2") or self.db.last_plan
        self.assertEqual(self.db.last_plan["index"], "idx_ab")

    def test_unique_wins_a_tie(self):
        self.db.run("CREATE INDEX zz_plain ON t (a, b)")
        self.db.run("CREATE UNIQUE INDEX aa_unique ON t (a, b)")
        self.db.run("SELECT * FROM t WHERE a = 1 AND b = 2")
        self.assertEqual(self.db.last_plan["index"], "aa_unique")

    def test_name_breaks_the_final_tie(self):
        self.db.run("CREATE INDEX zzz ON t (a)")
        self.db.run("CREATE INDEX aaa ON t (a)")
        self.db.run("SELECT * FROM t WHERE a = 1")
        self.assertEqual(self.db.last_plan["index"], "aaa")

    def test_choice_is_deterministic_across_runs(self):
        self.db.run("CREATE INDEX idx_a ON t (a)")
        first = self.db.run("SELECT * FROM t WHERE a = 1") and self.db.last_plan["index"]
        for _ in range(5):
            self.db.run("SELECT * FROM t WHERE a = 1")
            self.assertEqual(self.db.last_plan["index"], first)


if __name__ == "__main__":
    unittest.main()
