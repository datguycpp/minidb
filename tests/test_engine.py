"""Milestone 2 unit tests: statements execute over in-memory tables, and identity errors use
SQLite's own wording (the behaviour harness compares that text, so it is part of the contract).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.engine import Database, ExecError


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def run_sql(self, *statements):
        result = None
        for statement in statements:
            result = self.db.run(statement)
        return result


class TestCreate(DbTestCase):
    def test_creates_table(self):
        self.run_sql("CREATE TABLE t (id INTEGER, name TEXT)")
        self.assertIn("t", self.db.tables)

    def test_duplicate_table_is_an_error(self):
        self.run_sql("CREATE TABLE t (id INTEGER)")
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("CREATE TABLE t (id INTEGER)")
        self.assertEqual(str(ctx.exception), "table t already exists")

    def test_if_not_exists_is_silent(self):
        self.run_sql("CREATE TABLE t (id INTEGER)")
        self.assertIsNone(self.run_sql("CREATE TABLE IF NOT EXISTS t (id INTEGER)"))

    def test_duplicate_column_is_an_error(self):
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("CREATE TABLE t (id INTEGER, ID TEXT)")
        self.assertEqual(str(ctx.exception), "duplicate column name: ID")

    def test_primary_key_implies_not_null(self):
        self.run_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("INSERT INTO t VALUES (NULL)")
        self.assertEqual(str(ctx.exception), "NOT NULL constraint failed: t.id")


class TestInsert(DbTestCase):
    def setUp(self):
        super().setUp()
        self.run_sql("CREATE TABLE t (id INTEGER, name TEXT)")

    def test_insert_and_read_back(self):
        self.assertEqual(self.run_sql("INSERT INTO t VALUES (1, 'a')"), 1)
        self.assertEqual(self.run_sql("SELECT * FROM t"), (["id", "name"], [(1, "a")]))

    def test_wrong_arity_reports_sqlite_wording(self):
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("INSERT INTO t VALUES (1)")
        self.assertEqual(str(ctx.exception), "table t has 2 columns but 1 values were supplied")

    def test_unknown_column_reports_sqlite_wording(self):
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("INSERT INTO t (id, nope) VALUES (1, 'a')")
        self.assertEqual(str(ctx.exception), "table t has no column named nope")

    def test_omitted_column_defaults_to_null(self):
        self.run_sql("INSERT INTO t (id) VALUES (7)")
        self.assertEqual(self.run_sql("SELECT * FROM t")[1], [(7, None)])

    def test_default_applies_when_column_is_omitted(self):
        self.run_sql("CREATE TABLE u (n INTEGER DEFAULT 5, m TEXT)")
        self.run_sql("INSERT INTO u (m) VALUES ('x')")
        self.assertEqual(self.run_sql("SELECT n FROM u")[1], [(5,)])

    def test_explicit_null_overrides_default(self):
        self.run_sql("CREATE TABLE u (n INTEGER DEFAULT 5)")
        self.run_sql("INSERT INTO u VALUES (NULL)")
        self.assertEqual(self.run_sql("SELECT n FROM u")[1], [(None,)])

    def test_not_null_is_enforced(self):
        self.run_sql("CREATE TABLE u (n INTEGER NOT NULL)")
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("INSERT INTO u VALUES (NULL)")
        self.assertEqual(str(ctx.exception), "NOT NULL constraint failed: u.n")

    def test_unique_is_enforced(self):
        self.run_sql("CREATE TABLE u (n INTEGER UNIQUE)")
        self.run_sql("INSERT INTO u VALUES (1)")
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("INSERT INTO u VALUES (1)")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: u.n")

    def test_check_constraint_is_enforced(self):
        self.run_sql("CREATE TABLE u (n INTEGER CHECK (n > 0))")
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("INSERT INTO u VALUES (0)")
        # SQLite echoes the expression text, not the column name.
        self.assertEqual(str(ctx.exception), "CHECK constraint failed: n > 0")

    def test_no_such_table(self):
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("INSERT INTO nope VALUES (1)")
        self.assertEqual(str(ctx.exception), "no such table: nope")


class TestSelect(DbTestCase):
    def setUp(self):
        super().setUp()
        self.run_sql("CREATE TABLE t (id INTEGER, name TEXT)")
        self.run_sql("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, NULL)")

    def test_star_returns_all_columns(self):
        columns, rows = self.run_sql("SELECT * FROM t")
        self.assertEqual(columns, ["id", "name"])
        self.assertEqual(len(rows), 3)

    def test_projection(self):
        self.assertEqual(self.run_sql("SELECT name FROM t"), (["name"], [("a",), ("b",), (None,)]))

    def test_where_integer_comparison(self):
        self.assertEqual(self.run_sql("SELECT id FROM t WHERE id > 1")[1], [(2,), (3,)])

    def test_null_comparison_matches_nothing(self):
        self.assertEqual(self.run_sql("SELECT id FROM t WHERE name = NULL")[1], [])

    def test_is_null(self):
        self.assertEqual(self.run_sql("SELECT id FROM t WHERE name IS NULL")[1], [(3,)])

    def test_is_not_null(self):
        self.assertEqual(self.run_sql("SELECT id FROM t WHERE name IS NOT NULL")[1], [(1,), (2,)])

    def test_and_or_precedence(self):
        columns, rows = self.run_sql("SELECT id FROM t WHERE id = 1 OR id = 2 AND name = 'b'")
        self.assertEqual(rows, [(1,), (2,)])

    def test_not(self):
        self.assertEqual(self.run_sql("SELECT id FROM t WHERE NOT id = 2")[1], [(1,), (3,)])

    def test_column_heading_uses_alias(self):
        self.assertEqual(self.run_sql("SELECT id AS ident FROM t")[0], ["ident"])

    def test_distinct(self):
        self.run_sql("CREATE TABLE d (v INTEGER)")
        self.run_sql("INSERT INTO d VALUES (1), (1), (2)")
        self.assertEqual(self.run_sql("SELECT DISTINCT v FROM d")[1], [(1,), (2,)])

    def test_unknown_column_in_where(self):
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("SELECT * FROM t WHERE nope = 1")
        self.assertEqual(str(ctx.exception), "no such column: nope")

    def test_unknown_table(self):
        with self.assertRaises(ExecError) as ctx:
            self.run_sql("SELECT * FROM nope")
        self.assertEqual(str(ctx.exception), "no such table: nope")

    def test_empty_result_keeps_column_names(self):
        self.assertEqual(self.run_sql("SELECT id, name FROM t WHERE id > 99"), (["id", "name"], []))


class TestThreeValuedLogic(DbTestCase):
    def setUp(self):
        super().setUp()
        self.run_sql("CREATE TABLE t (v INTEGER)")
        self.run_sql("INSERT INTO t VALUES (1), (NULL)")

    def test_null_or_true_is_true(self):
        # NULL OR TRUE is TRUE, so every row survives.
        self.assertEqual(len(self.run_sql("SELECT v FROM t WHERE v = 1 OR v IS NULL")[1]), 2)

    def test_null_and_false_is_false(self):
        self.assertEqual(self.run_sql("SELECT v FROM t WHERE v = 2 AND v = 1")[1], [])

    def test_not_null_is_null_not_true(self):
        # NOT NULL is NULL, and WHERE keeps only TRUE rows.
        self.assertEqual(self.run_sql("SELECT v FROM t WHERE NOT v = 1")[1], [])


if __name__ == "__main__":
    unittest.main()
