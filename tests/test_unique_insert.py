"""Write-path item (ordered after M38): a column-level UNIQUE/PRIMARY KEY check is a set lookup.

The old check scanned the table for every inserted row, so loading n rows into a table with a
PRIMARY KEY cost O(n^2) — measured at 16k rows as 2 ms/insert and rising, which is why the
benchmark's per-workload table builds dominated its runtime. The constraint is membership, not
search: one lazy pass builds the bucket, each insert is O(1), and any other row change clears it.

These tests assert the algorithm (scan counts), not the clock, and then re-assert the behaviour
the scan used to provide, including the cases that only a stale bucket would break.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.engine import Database, ExecError  # noqa: E402


class TestNoScanPerInsert(unittest.TestCase):
    def test_inserting_n_rows_scans_the_table_once(self):
        db = Database()
        db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        scans = []
        original = Database._rows

        def counting(self, table):
            scans.append(table.name)
            return original(self, table)

        Database._rows = counting
        try:
            for i in range(500):
                db.run(f"INSERT INTO t VALUES ({i}, {i})")
        finally:
            Database._rows = original
        self.assertEqual(len(scans), 1, f"expected one lazy build, saw {len(scans)} scans")
        self.assertEqual(db.run("SELECT count(*) FROM t")[1], [(500,)])

    def test_a_constraint_that_is_not_hit_costs_nothing(self):
        db = Database()
        db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
        scans = []
        original = Database._rows
        Database._rows = lambda self, table: (scans.append(table.name), original(self, table))[1]
        try:
            for i in range(100):
                db.run(f"INSERT INTO t VALUES ({i}, {i})")
        finally:
            Database._rows = original
        self.assertEqual(scans, [], "no unique column, no bucket, no scan")


class TestBehaviourKept(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT UNIQUE, v INTEGER)")

    def insert(self, id_, name="x", v=0):
        self.db.run(f"INSERT INTO t (id, name, v) VALUES ({id_}, '{name}', {v})")

    def test_duplicate_still_fails_with_the_reference_message(self):
        self.insert(1)
        with self.assertRaises(ExecError) as ctx:
            self.insert(1)
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: t.id")

    def test_a_deleted_key_can_be_reused(self):
        self.insert(1)
        self.db.run("DELETE FROM t WHERE id = 1")
        self.insert(1)                       # a stale bucket would refuse this
        self.assertEqual(self.db.run("SELECT count(*) FROM t")[1], [(1,)])

    def test_an_updated_key_frees_the_old_value(self):
        self.insert(1, name="a")
        self.db.run("UPDATE t SET name = 'b' WHERE id = 1")
        self.insert(2, name="a")             # 'a' was freed by the update
        with self.assertRaises(ExecError):
            self.insert(3, name="b")

    def test_altering_the_table_clears_the_bucket(self):
        self.insert(1, name="a")
        self.db.run("ALTER TABLE t ADD COLUMN extra TEXT")
        self.insert(2, name="b")             # a stale bucket built before the ALTER would
        with self.assertRaises(ExecError):   # still be keyed on the old column list
            self.insert(1, name="c")

    def test_nocase_still_folds(self):
        self.db.run("CREATE TABLE u (name TEXT UNIQUE COLLATE NOCASE)")
        self.db.run("INSERT INTO u VALUES ('Alpha')")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("INSERT INTO u VALUES ('alpha')")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: u.name")

    def test_a_rejected_row_does_not_reserve_its_key(self):
        self.db.run("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        self.db.run("CREATE TABLE child (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id))")
        self.db.run("PRAGMA foreign_keys = ON")
        with self.assertRaises(ExecError):
            self.db.run("INSERT INTO child VALUES (1, 99)")      # foreign key fails after the
        self.db.run("INSERT INTO parent VALUES (99)")             # unique check passed
        self.db.run("INSERT INTO child VALUES (1, 99)")           # the key must still be free
        self.assertEqual(self.db.run("SELECT count(*) FROM child")[1], [(1,)])

    def test_unique_index_path_is_unchanged(self):
        self.db.run("CREATE TABLE w (a INTEGER, b INTEGER)")
        self.db.run("CREATE UNIQUE INDEX idx_ab ON w (a, b)")
        self.db.run("INSERT INTO w VALUES (1, 1)")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("INSERT INTO w VALUES (1, 1)")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: w.a, w.b")

    def test_nulls_keep_their_existing_semantics(self):
        # A NULL skips the column-level check (the old scan never ran for it either) and SQLite
        # allows many NULLs in a UNIQUE column, so the bucket must not treat None as a key.
        self.db.run("CREATE TABLE n (id INTEGER UNIQUE)")
        self.db.run("INSERT INTO n VALUES (NULL)")
        self.db.run("INSERT INTO n VALUES (NULL)")
        self.assertEqual(self.db.run("SELECT count(*) FROM n")[1], [(2,)])


if __name__ == "__main__":
    unittest.main()
