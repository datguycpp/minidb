"""Milestone 4 tests: indexes exist to change *how* rows are found, so every test here checks
either the plan actually taken or SQLite's behaviour being preserved."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.engine import Database, ExecError


class Base(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (id INTEGER, name TEXT)")
        for i in range(1, 6):
            self.db.run(f"INSERT INTO t VALUES ({i}, 'name-{i}')")


class TestIndexPlans(Base):
    def test_index_is_used_for_equality(self):
        self.db.run("CREATE INDEX idx ON t (id)")
        rows = self.db.run("SELECT name FROM t WHERE id = 3")[1]
        self.assertEqual(rows, [("name-3",)])
        self.assertEqual(self.db.last_plan["access"], "index")
        self.assertEqual(self.db.last_plan["index"], "idx")
        self.assertEqual(self.db.last_plan["rows_examined"], 1)

    def test_full_scan_without_index(self):
        self.db.run("SELECT name FROM t WHERE id = 3")
        self.assertEqual(self.db.last_plan["access"], "full")
        self.assertEqual(self.db.last_plan["rows_examined"], 5)

    def test_index_not_used_for_unindexed_column(self):
        self.db.run("CREATE INDEX idx ON t (id)")
        self.db.run("SELECT name FROM t WHERE name = 'name-3'")
        self.assertEqual(self.db.last_plan["access"], "full")

    def test_and_predicate_uses_index_then_filters(self):
        self.db.run("CREATE INDEX idx ON t (id)")
        rows = self.db.run("SELECT name FROM t WHERE id = 3 AND name = 'name-3'")[1]
        self.assertEqual(rows, [("name-3",)])
        self.assertEqual(self.db.last_plan["access"], "index")

    def test_composite_index_requires_all_columns(self):
        self.db.run("CREATE INDEX idx ON t (id, name)")
        self.db.run("SELECT * FROM t WHERE id = 3")
        self.assertEqual(self.db.last_plan["access"], "full", "prefix without all columns must not use the index")
        self.db.run("SELECT * FROM t WHERE id = 3 AND name = 'name-3'")
        self.assertEqual(self.db.last_plan["access"], "index")

    def test_rows_inserted_after_index_are_found(self):
        self.db.run("CREATE INDEX idx ON t (id)")
        self.db.run("INSERT INTO t VALUES (99, 'late')")
        self.assertEqual(self.db.run("SELECT name FROM t WHERE id = 99")[1], [("late",)])
        self.assertEqual(self.db.last_plan["access"], "index")


class TestUniqueIndexes(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE u (a INTEGER, b TEXT)")

    def test_duplicate_insert_is_rejected_with_sqlite_wording(self):
        self.db.run("CREATE UNIQUE INDEX ux ON u (a)")
        self.db.run("INSERT INTO u VALUES (1, 'x')")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("INSERT INTO u VALUES (1, 'y')")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: u.a")

    def test_duplicate_data_at_create_index_is_rejected(self):
        self.db.run("INSERT INTO u VALUES (1, 'x'), (1, 'y')")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("CREATE UNIQUE INDEX ux ON u (a)")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: u.a")
        self.assertEqual(self.db.tables["u"].indexes, {}, "a failed CREATE must not register a half-built index")

    def test_composite_unique_message_names_both_columns(self):
        self.db.run("CREATE UNIQUE INDEX ux ON u (a, b)")
        self.db.run("INSERT INTO u VALUES (1, 'x')")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("INSERT INTO u VALUES (1, 'x')")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: u.a, u.b")

    def test_nulls_do_not_conflict(self):
        self.db.run("CREATE UNIQUE INDEX ux ON u (a)")
        self.db.run("INSERT INTO u VALUES (NULL, 'x')")
        self.db.run("INSERT INTO u VALUES (NULL, 'y')")
        self.assertEqual(len(self.db.run("SELECT * FROM u")[1]), 2)

    def test_rejected_insert_leaves_no_row(self):
        self.db.run("CREATE UNIQUE INDEX ux ON u (a)")
        self.db.run("INSERT INTO u VALUES (1, 'x')")
        with self.assertRaises(ExecError):
            self.db.run("INSERT INTO u VALUES (1, 'y')")
        self.assertEqual(len(self.db.run("SELECT * FROM u")[1]), 1)


class TestIndexErrors(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (a INTEGER)")

    def test_unknown_column(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("CREATE INDEX i ON t (nope)")
        self.assertEqual(str(ctx.exception), "no such column: nope")

    def test_unknown_table(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("CREATE INDEX i ON missing (a)")
        self.assertEqual(str(ctx.exception), "no such table: missing")

    def test_duplicate_index_name(self):
        self.db.run("CREATE INDEX i ON t (a)")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("CREATE INDEX i ON t (a)")
        self.assertEqual(str(ctx.exception), "index i already exists")

    def test_if_not_exists_is_silent(self):
        self.db.run("CREATE INDEX i ON t (a)")
        self.assertIsNone(self.db.run("CREATE INDEX IF NOT EXISTS i ON t (a)"))


class TestIndexPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "idx.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_index_definitions_survive_reopen_and_are_used(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (id INTEGER, name TEXT)")
        for i in range(1, 6):
            db.run(f"INSERT INTO t VALUES ({i}, 'n{i}')")
        db.run("CREATE INDEX idx ON t (id)")
        db.close()

        again = Database(self.path)
        self.assertEqual(again.run("SELECT name FROM t WHERE id = 4")[1], [("n4",)])
        self.assertEqual(again.last_plan["access"], "index", "rebuilt index must actually be used")
        again.close()

    def test_unique_index_survives_reopen(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("CREATE UNIQUE INDEX ux ON t (a)")
        db.run("INSERT INTO t VALUES (1)")
        db.close()

        again = Database(self.path)
        with self.assertRaises(ExecError) as ctx:
            again.run("INSERT INTO t VALUES (1)")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: t.a")
        again.close()


if __name__ == "__main__":
    unittest.main()
