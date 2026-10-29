"""Milestone 6 tests: transactions.

Two kinds of evidence here. The unit tests pin our own semantics (rollback restores rows, schema
and indexes). The differential tests run the same script through SQLite and MiniDB and require
identical outcomes — because "we think rollback works" is not the claim being made.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402


class MemoryTransactions(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (a INTEGER)")

    def test_rollback_discards_insert(self):
        self.db.run("BEGIN")
        self.db.run("INSERT INTO t VALUES (1)")
        self.assertEqual(self.db.run("SELECT * FROM t")[1], [(1,)], "uncommitted rows are visible inside the transaction")
        self.db.run("ROLLBACK")
        self.assertEqual(self.db.run("SELECT * FROM t")[1], [])

    def test_commit_keeps_insert(self):
        self.db.run("BEGIN")
        self.db.run("INSERT INTO t VALUES (1)")
        self.db.run("COMMIT")
        self.assertEqual(self.db.run("SELECT * FROM t")[1], [(1,)])

    def test_rollback_discards_ddl(self):
        self.db.run("BEGIN")
        self.db.run("CREATE TABLE other (x INTEGER)")
        self.db.run("ROLLBACK")
        self.assertNotIn("other", self.db.tables)

    def test_rollback_restores_indexes(self):
        self.db.run("BEGIN")
        self.db.run("CREATE INDEX idx ON t (a)")
        self.db.run("ROLLBACK")
        self.assertEqual(self.db.tables["t"].indexes, {})

    def test_rollback_restores_uniqueness(self):
        self.db.run("CREATE UNIQUE INDEX ux ON t (a)")
        self.db.run("INSERT INTO t VALUES (1)")
        self.db.run("BEGIN")
        self.db.run("INSERT INTO t VALUES (2)")
        self.db.run("ROLLBACK")
        self.db.run("INSERT INTO t VALUES (2)")   # would collide if the rollback left the index entry
        self.assertEqual(sorted(self.db.run("SELECT * FROM t")[1]), [(1,), (2,)])

    def test_nested_begin_is_rejected(self):
        self.db.run("BEGIN")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("BEGIN")
        self.assertEqual(str(ctx.exception), "cannot start a transaction within a transaction")
        self.db.run("ROLLBACK")

    def test_commit_without_begin(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("COMMIT")
        self.assertEqual(str(ctx.exception), "cannot commit - no transaction is active")

    def test_rollback_without_begin(self):
        with self.assertRaises(ExecError) as ctx:
            self.db.run("ROLLBACK")
        self.assertEqual(str(ctx.exception), "cannot rollback - no transaction is active")

    def test_begin_forms_parse(self):
        for statement in ("BEGIN", "BEGIN TRANSACTION", "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"):
            self.db.run(statement)
            self.db.run("ROLLBACK")


class DiskTransactions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "tx.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_rollback_leaves_file_unchanged_across_reopen(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1)")
        db.run("BEGIN")
        db.run("INSERT INTO t VALUES (2)")
        db.run("ROLLBACK")
        db.close()

        again = Database(self.path)
        self.assertEqual(again.run("SELECT * FROM t")[1], [(1,)])
        again.close()

    def test_commit_survives_reopen(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("BEGIN")
        db.run("INSERT INTO t VALUES (7)")
        db.run("COMMIT")
        db.close()

        again = Database(self.path)
        self.assertEqual(again.run("SELECT * FROM t")[1], [(7,)])
        again.close()

    def test_closing_with_open_transaction_rolls_back(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (a INTEGER)")
        db.run("INSERT INTO t VALUES (1)")
        db.run("BEGIN")
        db.run("INSERT INTO t VALUES (2)")
        db.close()                      # SQLite rolls back on connection close

        again = Database(self.path)
        self.assertEqual(again.run("SELECT * FROM t")[1], [(1,)])
        again.close()

    def test_rollback_restores_schema(self):
        db = Database(self.path)
        db.run("BEGIN")
        db.run("CREATE TABLE ghost (x INTEGER)")
        db.run("ROLLBACK")
        db.close()

        again = Database(self.path)
        self.assertEqual(again.tables, {})
        again.close()


class DifferentialTransactions(unittest.TestCase):
    """Same script, both engines, identical outcome — the claim the corpus will eventually carry."""

    def assert_same(self, statements):
        expected = bc.run_sqlite([statements])
        actual = bc.run_minidb([statements])
        ok, detail = bc.compare(expected, actual)
        self.assertTrue(ok, f"{statements} -> {detail}")

    def test_rollback_script(self):
        self.assert_same(["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1)",
                          "BEGIN", "INSERT INTO t VALUES (2)", "ROLLBACK", "SELECT * FROM t"])

    def test_commit_script(self):
        self.assert_same(["CREATE TABLE t (a INTEGER)", "BEGIN", "INSERT INTO t VALUES (1)",
                          "COMMIT", "SELECT * FROM t"])

    def test_rollback_of_ddl(self):
        self.assert_same(["BEGIN", "CREATE TABLE t (a INTEGER)", "ROLLBACK", "SELECT * FROM t"])

    def test_commit_without_begin(self):
        self.assert_same(["COMMIT"])

    def test_rollback_without_begin(self):
        self.assert_same(["ROLLBACK"])

    def test_nested_begin(self):
        self.assert_same(["BEGIN", "BEGIN"])

    def test_rollback_after_multiple_statements(self):
        self.assert_same(["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (0)",
                          "BEGIN", "INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)",
                          "ROLLBACK", "SELECT * FROM t"])


if __name__ == "__main__":
    unittest.main()
