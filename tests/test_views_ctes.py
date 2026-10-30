"""Milestone 14 tests: views and CTEs (windows are milestone 15)."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402

SEED = [
    "CREATE TABLE t (id INTEGER, v INTEGER)",
    "INSERT INTO t VALUES (1, 10), (2, 20), (3, 30)",
]


class TestViews(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SEED:
            self.db.run(statement)

    def test_plain_view(self):
        self.db.run("CREATE VIEW v AS SELECT id FROM t")
        self.assertEqual(self.db.run("SELECT * FROM v ORDER BY id")[1], [(1,), (2,), (3,)])

    def test_view_column_aliases(self):
        self.db.run("CREATE VIEW renamed (first, second) AS SELECT id, v FROM t")
        columns, rows = self.db.run("SELECT first FROM renamed ORDER BY first")
        self.assertEqual(columns, ["first"])
        self.assertEqual(rows, [(1,), (2,), (3,)])

    def test_view_over_join(self):
        self.db.run("CREATE TABLE u (id INTEGER, label TEXT)")
        self.db.run("INSERT INTO u VALUES (1, 'a'), (3, 'c')")
        self.db.run("CREATE VIEW j AS SELECT t.id, u.label FROM t JOIN u ON t.id = u.id")
        self.assertEqual(self.db.run("SELECT * FROM j ORDER BY id")[1], [(1, "a"), (3, "c")])

    def test_nested_view(self):
        self.db.run("CREATE VIEW v1 AS SELECT id, v FROM t")
        self.db.run("CREATE VIEW v2 AS SELECT id FROM v1 WHERE v > 10")
        self.assertEqual(self.db.run("SELECT * FROM v2 ORDER BY id")[1], [(2,), (3,)])

    def test_create_twice_uses_reference_wording(self):
        self.db.run("CREATE VIEW v AS SELECT id FROM t")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("CREATE VIEW v AS SELECT id FROM t")
        self.assertEqual(str(ctx.exception), "view v already exists")

    def test_if_not_exists_is_silent(self):
        self.db.run("CREATE VIEW v AS SELECT id FROM t")
        self.assertIsNone(self.db.run("CREATE VIEW IF NOT EXISTS v AS SELECT 9"))

    def test_views_are_read_only(self):
        self.db.run("CREATE VIEW v AS SELECT id FROM t")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("INSERT INTO v VALUES (4)")
        self.assertEqual(str(ctx.exception), "cannot modify v because it is a view")

    def test_drop_view(self):
        self.db.run("CREATE VIEW v AS SELECT id FROM t")
        self.db.run("DROP VIEW v")
        with self.assertRaises(ExecError):
            self.db.run("SELECT * FROM v")


class TestCTEs(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SEED:
            self.db.run(statement)

    def test_simple_cte(self):
        self.assertEqual(self.db.run("WITH big AS (SELECT id FROM t WHERE v > 15) "
                                     "SELECT * FROM big ORDER BY id")[1], [(2,), (3,)])

    def test_chained_ctes(self):
        rows = self.db.run("WITH a AS (SELECT id, v FROM t), b AS (SELECT id FROM a WHERE v > 15) "
                           "SELECT * FROM b ORDER BY id")[1]
        self.assertEqual(rows, [(2,), (3,)])

    def test_cte_read_twice(self):
        rows = self.db.run("WITH c AS (SELECT v FROM t) "
                           "SELECT (SELECT COUNT(*) FROM c), (SELECT SUM(v) FROM c)")[1]
        self.assertEqual(rows, [(3, 60)])

    def test_recursive_counter(self):
        rows = self.db.run("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL "
                           "SELECT i + 1 FROM n WHERE i < 5) SELECT i FROM n ORDER BY i")[1]
        self.assertEqual(rows, [(1,), (2,), (3,), (4,), (5,)])

    def test_recursive_empty_base(self):
        rows = self.db.run("WITH RECURSIVE n(i) AS (SELECT 1 WHERE 0 UNION ALL "
                           "SELECT i + 1 FROM n) SELECT i FROM n")[1]
        self.assertEqual(rows, [])

    def test_recursive_tree_walk(self):
        self.db.run("CREATE TABLE tree (id INTEGER, parent INTEGER)")
        self.db.run("INSERT INTO tree VALUES (1, NULL), (2, 1), (3, 1), (4, 2)")
        rows = self.db.run("WITH RECURSIVE walk(id, depth) AS ("
                           "SELECT id, 0 FROM tree WHERE parent IS NULL UNION ALL "
                           "SELECT tree.id, walk.depth + 1 FROM tree JOIN walk ON tree.parent = walk.id"
                           ") SELECT id, depth FROM walk ORDER BY id")[1]
        self.assertEqual(rows, [(1, 0), (2, 1), (3, 1), (4, 2)])

    def test_cte_shadow_does_not_persist(self):
        self.db.run("WITH x AS (SELECT 1) SELECT * FROM x")
        with self.assertRaises(ExecError):
            self.db.run("SELECT * FROM x")


class DifferentialM14(unittest.TestCase):
    CASES = [
        SEED + ["CREATE VIEW v AS SELECT id FROM t", "SELECT * FROM v ORDER BY id"],
        SEED + ["CREATE VIEW r (a, b) AS SELECT id, v FROM t", "SELECT a, b FROM r ORDER BY a"],
        SEED + ["CREATE VIEW v AS SELECT id FROM t", "CREATE VIEW v2 AS SELECT id FROM v WHERE id > 1",
                "SELECT * FROM v2 ORDER BY id"],
        SEED + ["CREATE VIEW v AS SELECT id FROM t", "INSERT INTO v VALUES (9)"],
        SEED + ["CREATE VIEW v AS SELECT id FROM t", "SELECT * FROM v ORDER BY id LIMIT 2"],
        SEED + ["WITH big AS (SELECT id FROM t WHERE v > 15) SELECT * FROM big ORDER BY id"],
        SEED + ["WITH a AS (SELECT id, v FROM t), b AS (SELECT id FROM a WHERE v > 15) "
                "SELECT * FROM b ORDER BY id"],
        SEED + ["WITH c AS (SELECT v FROM t) SELECT (SELECT COUNT(*) FROM c), (SELECT SUM(v) FROM c)"],
        SEED + ["WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) "
                "SELECT i FROM n ORDER BY i"],
        SEED + ["WITH x AS (SELECT 1) SELECT * FROM x"],
        SEED + ["CREATE VIEW v AS SELECT id FROM t", "DROP VIEW v", "SELECT * FROM v"],
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
