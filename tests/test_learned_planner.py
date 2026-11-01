"""Milestone 37 tests: the planner remembers plans and prices covering paths honestly.

Two things changed, and they are different in kind:

1. **Covering paths are no longer charged for row fetches they never make.** The M21 cost rule
   (a random fetch costs RANDOM_ACCESS_COST sequential reads) was written before M22 added
   covering, so a covering index over a wide range was sent to a scan that reads pages it did not
   need to read. Coverage is a property of the query, so the decision takes the projection too.
2. **The planner remembers the plan it chose for a statement.** The estimate is exact (a hash
   index reports its bucket size), so history cannot improve a *choice* — what it can do is stop
   paying for the same O(distinct keys) range probe on every execution. Any write clears the
   memory: a decision carries the rowid set it was made from.

The conformance invariant is the point of both: they change *choices* only. Every test that
changes a plan also checks the rows.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.engine import Database  # noqa: E402


def build(rows=400, distinct=400):
    db = Database()
    db.run("CREATE TABLE t (id INTEGER, v INTEGER, pad TEXT)")
    for i in range(1, rows + 1):
        db.run(f"INSERT INTO t VALUES ({i}, {i % distinct}, 'p{i % 3}')")
    db.run("CREATE INDEX idx_v ON t (v)")
    return db


class TestCoveringCostRule(unittest.TestCase):
    def test_wide_covering_range_uses_the_index(self):
        db = build()
        db.run("SELECT v FROM t WHERE v >= 380")
        plan = db.last_plan
        self.assertEqual(plan["access"], "index", "a covering range pays no row fetch")
        self.assertEqual(plan["reason"], "covering-index")

    def test_the_same_range_without_coverage_still_scans(self):
        db = build()
        db.run("SELECT id FROM t WHERE v >= 380")
        self.assertEqual(db.last_plan["access"], "full")
        self.assertEqual(db.last_plan["reason"], "index-not-selective",
                         "the same range is a scan when the row is needed: the rule is about cost")

    def test_coverage_does_not_change_answers(self):
        db = build()
        covering = db.run("SELECT v FROM t WHERE v >= 380")[1]
        scanning = db.run("SELECT id, v, pad FROM t WHERE v >= 380")[1]
        expected = [(row[0],) for row in scanning]
        self.assertEqual(covering, expected)

    def test_a_full_range_is_still_a_wash_and_may_scan(self):
        db = build()
        db.run("SELECT v FROM t WHERE v >= 0")
        # Coverage makes the index affordable, but the plan must stay answer-correct either way.
        self.assertEqual(db.run("SELECT count(*) FROM t WHERE v >= 0")[1], [(400,)])


class TestRememberedPlans(unittest.TestCase):
    def test_a_repeated_statement_is_remembered(self):
        db = build()
        db.run("SELECT v FROM t WHERE v >= 300")
        self.assertFalse(db.last_plan["remembered"])
        db.run("SELECT v FROM t WHERE v >= 300")
        self.assertTrue(db.last_plan["remembered"])

    def test_a_write_clears_remembered_plans(self):
        db = build()
        db.run("SELECT v FROM t WHERE v >= 300")
        db.run("SELECT v FROM t WHERE v >= 300")
        self.assertTrue(db.last_plan["remembered"])
        db.run("INSERT INTO t VALUES (999, 999, 'late')")
        db.run("SELECT v FROM t WHERE v >= 300")
        self.assertFalse(db.last_plan["remembered"],
                         "a remembered plan carries the rowids it was chosen from")

    def test_a_rollback_still_clears(self):
        db = build()
        db.run("SELECT v FROM t WHERE v >= 300")
        db.run("BEGIN")
        db.run("INSERT INTO t VALUES (998, 998, 'gone')")
        db.run("ROLLBACK")
        db.run("SELECT v FROM t WHERE v >= 300")
        self.assertFalse(db.last_plan["remembered"], "over-invalidating is cheap; staleness is not")

    def test_ddl_clears_and_the_new_index_is_visible(self):
        db = build()
        db.run("SELECT id FROM t WHERE pad = 'p1'")
        self.assertFalse(db.last_plan["remembered"])
        db.run("CREATE INDEX idx_pad ON t (pad)")
        db.run("SELECT id FROM t WHERE pad = 'p1'")
        self.assertFalse(db.last_plan["remembered"])
        self.assertEqual(db.last_plan["access"], "index", "the plan must see the new index")

    def test_read_only_statements_keep_the_memory(self):
        db = build()
        db.run("SELECT v FROM t WHERE v >= 300")
        db.run("PRAGMA table_info(t)")
        db.run("SELECT v FROM t WHERE v >= 300")
        self.assertTrue(db.last_plan["remembered"])

    def test_projection_is_part_of_the_key(self):
        db = build()
        db.run("SELECT v FROM t WHERE v >= 300")          # covering decision
        self.assertEqual(db.last_plan["access"], "index")
        db.run("SELECT * FROM t WHERE v >= 300")          # not covered: must be planned afresh
        self.assertFalse(db.last_plan["remembered"])
        self.assertEqual(db.last_plan["access"], "full")

    def test_two_predicates_on_one_table_do_not_share_an_entry(self):
        db = build()
        db.run("SELECT v FROM t WHERE v >= 300")
        db.run("SELECT v FROM t WHERE v < 5")
        self.assertFalse(db.last_plan["remembered"],
                         "different predicates are different decisions")

    def test_answers_are_identical_with_and_without_the_memory(self):
        db = build()
        fresh = db.run("SELECT id, v FROM t WHERE v >= 380 ORDER BY id")[1]
        remembered = db.run("SELECT id, v FROM t WHERE v >= 380 ORDER BY id")[1]
        self.assertEqual(fresh, remembered)
        self.assertTrue(db.last_plan["remembered"])

    def test_the_cache_is_bounded_and_keeps_answering(self):
        db = build()
        for i in range(200):
            rows = db.run(f"SELECT id FROM t WHERE v = {i}")[1]
            self.assertEqual(len(rows), 1)
        self.assertLessEqual(len(db._plan_cache), 128)


if __name__ == "__main__":
    unittest.main()
