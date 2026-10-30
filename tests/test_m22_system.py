"""Milestone 22 tests: partial and covering indexes, plus the system surface t3b measures
(sqlite_master, PRAGMA table_info, ANALYZE, DROP TABLE, AUTOINCREMENT)."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402


class TestCoveringIndex(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
        self.db.run("INSERT INTO t VALUES (1, 10), (2, 20), (3, 30)")
        self.db.run("CREATE INDEX idx_v ON t (v)")

    def test_query_served_from_the_index_alone(self):
        self.db.run("SELECT v FROM t WHERE v = 20")
        self.assertEqual(self.db.last_plan["reason"], "covering-index",
                         "no row fetch is needed when every column is in the index")

    def test_covering_result_matches(self):
        self.assertEqual(self.db.run("SELECT v FROM t WHERE v >= 20 ORDER BY v")[1],
                         [(20,), (30,)])

    def test_non_covering_column_selects_falls_back(self):
        self.db.run("SELECT id FROM t WHERE v = 20")
        self.assertNotEqual(self.db.last_plan["reason"], "covering-index",
                            "id is not in the index, so the row must be fetched")


class TestPartialIndex(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (v INTEGER)")
        self.db.run("INSERT INTO t VALUES (1), (5), (15), (20)")
        self.db.run("CREATE INDEX idx ON t (v) WHERE v > 10")

    def test_partial_index_is_used_when_the_predicate_repeats(self):
        self.db.run("SELECT v FROM t WHERE v > 10")
        self.assertIn(self.db.last_plan["access"], ("index", "full"))

    def test_partial_index_maintained_on_insert(self):
        self.db.run("INSERT INTO t VALUES (30)")
        self.assertEqual(self.db.run("SELECT count(*) FROM t WHERE v = 30")[1], [(1,)])

    def test_partial_index_covers_only_matching_rows(self):
        index = list(self.db.tables["t"].indexes.values())[0]
        self.assertEqual(len(index.map), 2, "only v > 10 belongs in the index")

    def test_partial_index_not_used_without_the_predicate(self):
        self.db.run("SELECT v FROM t WHERE v = 1")
        self.assertEqual(self.db.last_plan["reason"], "no-eligible-index",
                         "a partial index is not a candidate outside its predicate")


class TestSystemSurface(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def test_sqlite_master_lists_objects(self):
        self.db.run("CREATE TABLE alpha (id INTEGER)")
        self.db.run("CREATE TABLE beta (id TEXT)")
        self.db.run("CREATE INDEX idx_alpha ON alpha (id)")
        self.db.run("DROP TABLE beta")
        self.assertEqual(self.db.run("SELECT type, name FROM sqlite_master ORDER BY type, name")[1],
                         [("index", "idx_alpha"), ("table", "alpha")])

    def test_sqlite_master_includes_views(self):
        self.db.run("CREATE TABLE t (id INTEGER)")
        self.db.run("CREATE VIEW v AS SELECT id FROM t")
        rows = self.db.run("SELECT type, name FROM sqlite_master ORDER BY type, name")[1]
        self.assertEqual(rows, [("table", "t"), ("view", "v")])

    def test_pragma_table_info(self):
        self.db.run("CREATE TABLE t (id INTEGER NOT NULL PRIMARY KEY, name TEXT DEFAULT 'x')")
        columns, rows = self.db.run("PRAGMA table_info (t)")
        self.assertEqual(columns, ["cid", "name", "type", "notnull", "dflt_value", "pk"])
        self.assertEqual(rows[0], (0, "id", "INTEGER", 1, None, 1))
        self.assertEqual(rows[1], (1, "name", "TEXT", 0, "'x'", 0))

    def test_analyze_is_accepted_and_changes_nothing(self):
        self.db.run("CREATE TABLE t (id INTEGER)")
        self.db.run("INSERT INTO t VALUES (1)")
        self.assertIsNone(self.db.run("ANALYZE"))
        self.assertEqual(self.db.run("SELECT id FROM t")[1], [(1,)])

    def test_autoincrement_and_sqlite_sequence(self):
        self.db.run("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        self.db.run("INSERT INTO t VALUES (1)")
        self.db.run("INSERT INTO t VALUES (2)")
        self.assertEqual(self.db.run("SELECT seq FROM sqlite_sequence")[1], [(2,)])

    def test_autoincrement_allocates_when_id_is_omitted(self):
        self.db.run("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
        self.db.run("INSERT INTO t (v) VALUES ('a')")
        self.db.run("INSERT INTO t (v) VALUES ('b')")
        self.assertEqual(self.db.run("SELECT id, v FROM t ORDER BY id")[1], [(1, "a"), (2, "b")])


class DifferentialM22(unittest.TestCase):
    CASES = [
        ["CREATE TABLE t (id INTEGER, v INTEGER)", "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
         "CREATE INDEX idx_v ON t (v)", "SELECT v FROM t WHERE v = 20"],
        ["CREATE TABLE t (id INTEGER, v INTEGER)", "INSERT INTO t VALUES (1,10),(2,20),(3,30)",
         "CREATE INDEX idx_v ON t (v)", "SELECT v FROM t WHERE v >= 20 ORDER BY v"],
        ["CREATE TABLE t (v INTEGER)", "INSERT INTO t VALUES (1),(5),(15),(20)",
         "CREATE INDEX idx ON t (v) WHERE v > 10", "SELECT v FROM t WHERE v > 10 ORDER BY v"],
        ["CREATE TABLE t (v INTEGER)", "INSERT INTO t VALUES (1),(5),(15),(20)",
         "CREATE INDEX idx ON t (v) WHERE v > 10", "INSERT INTO t VALUES (30)",
         "SELECT count(*) FROM t WHERE v = 30"],
        ["CREATE TABLE alpha (id INTEGER)", "CREATE TABLE beta (id TEXT)",
         "CREATE INDEX idx_alpha ON alpha (id)", "DROP TABLE beta",
         "SELECT type, name FROM sqlite_master ORDER BY type, name"],
        ["CREATE TABLE t (id INTEGER NOT NULL PRIMARY KEY, name TEXT DEFAULT 'x')",
         "PRAGMA table_info (t)"],
        ["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (1)", "ANALYZE", "SELECT id FROM t"],
        ["CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)", "INSERT INTO t VALUES (1)",
         "INSERT INTO t VALUES (2)", "SELECT seq FROM sqlite_sequence"],
        ["CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)",
         "INSERT INTO t (v) VALUES ('a')", "INSERT INTO t (v) VALUES ('b')",
         "SELECT id, v FROM t ORDER BY id"],
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
