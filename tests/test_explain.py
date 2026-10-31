"""Milestone 32 tests: EXPLAIN, human-readable only.

EXPLAIN is explicitly not a conformance surface (Tier-5 ruling): SQLite's output is its VM's
bytecode and its planner's vocabulary. These tests assert our plan is *informative and truthful* —
it changes when an index makes a lookup possible — not that it matches the reference.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402
from minidb.sql.parser import ParseError  # noqa: E402


def details(db, sql):
    columns, rows = db.run(sql)
    return columns, [row[-1] for row in rows]        # detail is the last column


class TestExplain(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (a INTEGER, b TEXT)")
        self.db.run("INSERT INTO t VALUES " + ",".join(f"({i}, 'v{i}')" for i in range(200)))

    def test_plan_columns_mirror_query_plan_shape(self):
        columns, _ = details(self.db, "EXPLAIN QUERY PLAN SELECT * FROM t WHERE b = 'x'")
        self.assertEqual(columns, ["id", "parent", "notused", "detail"])

    def test_scan_reports_its_reason(self):
        _, plan = details(self.db, "EXPLAIN QUERY PLAN SELECT * FROM t WHERE b = 'v42'")
        self.assertEqual(plan[0], "SCAN t")
        self.assertIn("no-eligible-index", plan[1])

    def test_index_changes_the_plan(self):
        before = details(self.db, "EXPLAIN QUERY PLAN SELECT * FROM t WHERE a = 42")[1]
        self.assertEqual(before[0], "SCAN t")
        self.db.run("CREATE INDEX idx_a ON t (a)")
        after = details(self.db, "EXPLAIN QUERY PLAN SELECT * FROM t WHERE a = 42")[1]
        self.assertEqual(after[0], "SEARCH t USING INDEX idx_a",
                         "the plan must react to the index, or it is decoration")

    def test_join_reports_a_nested_loop_with_its_tables(self):
        self.db.run("CREATE TABLE u (a INTEGER)")
        _, plan = details(self.db, "EXPLAIN QUERY PLAN SELECT * FROM t, u WHERE t.a = u.a")
        self.assertEqual(plan[0], "NESTED LOOP")
        self.assertIn("  TABLE t", plan)
        self.assertIn("  TABLE u", plan)

    def test_explain_does_not_claim_bytecode(self):
        _, plan = details(self.db, "EXPLAIN SELECT * FROM t")
        joined = " ".join(plan).lower()
        for word in ("opcode", "bytecode", "addr", "p1", "p2", "p3"):
            self.assertNotIn(word, joined,
                             "EXPLAIN must not imitate SQLite's VM vocabulary")

    def test_explain_of_a_write_is_declared_unsupported(self):
        _, plan = details(self.db, "EXPLAIN INSERT INTO t VALUES (1, 'x')")
        self.assertIn("only SELECT plans", plan[0])

    def test_explain_plan_of_a_compound(self):
        _, plan = details(self.db, "EXPLAIN QUERY PLAN SELECT a FROM t UNION SELECT a FROM t")
        self.assertEqual(plan[0], "COMPOUND UNION")


if __name__ == "__main__":
    unittest.main()
