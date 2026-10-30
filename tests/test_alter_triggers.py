"""Milestone 16 tests: ALTER TABLE, triggers, and the two carried debts
(views surviving reopen, UPDATE/DELETE on disk-backed tables)."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402


class TestAlter(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (a INTEGER)")
        self.db.run("INSERT INTO t VALUES (1), (2)")

    def test_add_column_with_default(self):
        self.db.run("ALTER TABLE t ADD COLUMN b TEXT DEFAULT 'x'")
        self.assertEqual(self.db.run("SELECT a, b FROM t ORDER BY a")[1], [(1, "x"), (2, "x")])

    def test_add_column_without_default_is_null(self):
        self.db.run("ALTER TABLE t ADD COLUMN c INTEGER")
        self.assertEqual(self.db.run("SELECT c FROM t ORDER BY a")[1], [(None,), (None,)])

    def test_add_not_null_without_default_is_refused(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("ALTER TABLE t ADD COLUMN d INTEGER NOT NULL")
        self.assertEqual(str(ctx.exception),
                         "Cannot add a NOT NULL column with default value NULL")

    def test_rename_column(self):
        self.db.run("ALTER TABLE t RENAME COLUMN a TO z")
        self.assertEqual(self.db.run("SELECT z FROM t ORDER BY z")[1], [(1,), (2,)])

    def test_drop_column(self):
        self.db.run("ALTER TABLE t ADD COLUMN b INTEGER DEFAULT 7")
        self.db.run("ALTER TABLE t DROP COLUMN b")
        columns, rows = self.db.run("SELECT * FROM t")
        self.assertEqual(columns, ["a"])
        self.assertEqual(rows, [(1,), (2,)])

    def test_rename_table(self):
        self.db.run("ALTER TABLE t RENAME TO renamed")
        self.assertEqual(self.db.run("SELECT count(*) FROM renamed")[1], [(2,)])


class TestTriggers(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (id INTEGER, a INTEGER)")
        self.db.run("CREATE TABLE log (old_v INTEGER, new_v INTEGER)")
        self.db.run("INSERT INTO t VALUES (1, 10)")

    def test_after_update_writes_log(self):
        self.db.run("CREATE TRIGGER tr AFTER UPDATE ON t BEGIN "
                    "INSERT INTO log VALUES (OLD.a, NEW.a); END")
        self.db.run("UPDATE t SET a = 20 WHERE id = 1")
        self.assertEqual(self.db.run("SELECT * FROM log")[1], [(10, 20)])

    def test_when_gate_blocks_the_body(self):
        self.db.run("CREATE TRIGGER tr AFTER UPDATE ON t WHEN NEW.a > 100 BEGIN "
                    "INSERT INTO log VALUES (OLD.a, NEW.a); END")
        self.db.run("UPDATE t SET a = 50 WHERE id = 1")
        self.assertEqual(self.db.run("SELECT count(*) FROM log")[1], [(0,)])
        self.db.run("UPDATE t SET a = 200 WHERE id = 1")
        self.assertEqual(self.db.run("SELECT count(*) FROM log")[1], [(1,)])

    def test_raise_abort_surfaces_its_message(self):
        self.db.run("CREATE TRIGGER guard BEFORE DELETE ON t BEGIN "
                    "SELECT RAISE(ABORT, 'no deletes'); END")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("DELETE FROM t")
        self.assertEqual(str(ctx.exception), "no deletes")
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(1,)], "row survives")

    def test_drop_trigger_silences_it(self):
        self.db.run("CREATE TRIGGER guard BEFORE DELETE ON t BEGIN "
                    "SELECT RAISE(ABORT, 'no deletes'); END")
        self.db.run("DROP TRIGGER guard")
        self.db.run("DELETE FROM t")
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(0,)])


class TestCarriedDebts(unittest.TestCase):
    def test_view_survives_reopen(self):
        path = os.path.join(tempfile.mkdtemp(), "views.db")
        db = Database(path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1), (2)")
        db.run("CREATE VIEW v AS SELECT a FROM t WHERE a > 1")
        db.close()
        again = Database(path)
        self.assertEqual(again.run("SELECT * FROM v")[1], [(2,)])
        again.close()

    def test_disk_update_and_delete_persist(self):
        path = os.path.join(tempfile.mkdtemp(), "dml.db")
        db = Database(path)
        db.run("CREATE TABLE t (a INTEGER, b TEXT)")
        db.run("INSERT INTO t VALUES (1, 'x'), (2, 'y'), (3, 'z')")
        db.run("UPDATE t SET b = 'Q' WHERE a = 2")
        db.run("DELETE FROM t WHERE a = 3")
        db.close()
        again = Database(path)
        self.assertEqual(again.run("SELECT a, b FROM t ORDER BY a")[1], [(1, "x"), (2, "Q")])
        again.close()


class DifferentialM16(unittest.TestCase):
    CASES = [
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1),(2)",
         "ALTER TABLE t ADD COLUMN b TEXT DEFAULT 'x'", "SELECT a, b FROM t ORDER BY a"],
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1),(2)",
         "ALTER TABLE t RENAME COLUMN a TO z", "SELECT z FROM t ORDER BY z"],
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1)",
         "ALTER TABLE t ADD COLUMN c INTEGER NOT NULL"],
        ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1),(2)",
         "ALTER TABLE t RENAME TO renamed", "SELECT count(*) FROM renamed"],
        ["CREATE TABLE t (id INTEGER, a INTEGER)", "CREATE TABLE log (old_v INTEGER, new_v INTEGER)",
         "INSERT INTO t VALUES (1, 10)",
         "CREATE TRIGGER tr AFTER UPDATE ON t BEGIN INSERT INTO log VALUES (OLD.a, NEW.a); END",
         "UPDATE t SET a = 20 WHERE id = 1", "SELECT * FROM log"],
        ["CREATE TABLE t (id INTEGER, a INTEGER)", "CREATE TABLE log (old_v INTEGER, new_v INTEGER)",
         "INSERT INTO t VALUES (1, 10)",
         "CREATE TRIGGER tr AFTER UPDATE ON t WHEN NEW.a > 100 BEGIN "
         "INSERT INTO log VALUES (OLD.a, NEW.a); END",
         "UPDATE t SET a = 50 WHERE id = 1", "SELECT count(*) FROM log"],
        ["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (1)",
         "CREATE TRIGGER guard BEFORE DELETE ON t BEGIN SELECT RAISE(ABORT, 'no deletes'); END",
         "DELETE FROM t", "SELECT count(*) FROM t"],
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
