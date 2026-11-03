"""The rowid namespace: allocation, holes, renumbering, and the alias.

The conformance surface is t6d.txt; these tests pin the parts a corpus case cannot reach — a
rowid surviving reopen on disk, and the plan actually taking the rowid path rather than a scan.
The plan assertions are the acceptance's unit form: W1 and W8 are the same query shapes at 100k,
so if the access path regresses here it regresses there too.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402


class RowidTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def rows(self, statement):
        return self.db.run(statement)[1]

    def test_allocation_is_max_plus_one_and_skips_holes(self):
        self.db.run("CREATE TABLE t (a TEXT)")
        self.db.run("INSERT INTO t VALUES ('x'), ('y'), ('z')")
        self.db.run("DELETE FROM t WHERE a = 'y'")
        self.db.run("INSERT INTO t VALUES ('w')")
        self.assertEqual(self.rows("SELECT rowid, a FROM t ORDER BY rowid"),
                         [(1, "x"), (3, "z"), (4, "w")])

    def test_an_explicit_rowid_wins_and_a_duplicate_is_refused(self):
        self.db.run("CREATE TABLE t (a TEXT)")
        self.db.run("INSERT INTO t (rowid, a) VALUES (42, 'x')")
        self.assertEqual(self.rows("SELECT rowid FROM t"), [(42,)])
        with self.assertRaises(Exception) as caught:
            self.db.run("INSERT INTO t (rowid, a) VALUES (42, 'y')")
        self.assertIn("UNIQUE constraint failed: t.rowid", str(caught.exception))

    def test_a_real_column_named_rowid_shadows_the_namespace(self):
        self.db.run("CREATE TABLE s (rowid TEXT, a TEXT)")
        self.db.run("INSERT INTO s VALUES ('shadow', 'v')")
        self.assertEqual(self.rows("SELECT rowid, a FROM s"), [("shadow", "v")])

    def test_the_integer_primary_key_is_the_rowid(self):
        self.db.run("CREATE TABLE p (id INTEGER PRIMARY KEY, a TEXT)")
        self.db.run("INSERT INTO p VALUES (7, 'x')")
        self.assertEqual(self.rows("SELECT rowid, id FROM p"), [(7, 7)])

    def test_a_point_lookup_and_an_update_use_the_rowid_path(self):
        self.db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER, k TEXT)")
        for i in range(1, 50):
            self.db.run(f"INSERT INTO t VALUES ({i}, {i}, 'k{i}')")
        self.db.run("SELECT k FROM t WHERE id = 7")
        self.assertEqual(self.db.last_plan["access"], "rowid")
        self.db.run("UPDATE t SET v = 99 WHERE id = 7")
        self.assertEqual(self.db.last_plan["access"], "rowid")
        self.assertEqual(self.rows("SELECT v FROM t WHERE id = 7"), [(99,)])

    def test_holes_survive_a_reopen_and_vacuum_renumbers(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "rowid.db")
            db = Database(path)
            db.run("CREATE TABLE t (a TEXT)")
            db.run("INSERT INTO t VALUES ('x'), ('y'), ('z')")
            db.run("DELETE FROM t WHERE a = 'y'")
            self.assertEqual(db.run("SELECT rowid FROM t ORDER BY rowid")[1], [(1,), (3,)])
            db.close()

            db = Database(path)
            self.assertEqual(db.run("SELECT rowid FROM t ORDER BY rowid")[1], [(1,), (3,)])
            db.run("VACUUM")
            self.assertEqual(db.run("SELECT rowid FROM t ORDER BY rowid")[1], [(1,), (2,)])
            db.close()

            db = Database(path)
            self.assertEqual(db.run("SELECT rowid FROM t ORDER BY rowid")[1], [(1,), (2,)])
            db.close()

    def test_an_omitted_key_is_assigned_before_the_not_null_check(self):
        # Skeptic round 1, case 27: the NOT NULL check ran before the alias was assigned, so a
        # legal `INSERT INTO t (s) VALUES (...)` reported "NOT NULL constraint failed: t.id".
        self.db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT)")
        self.db.run("INSERT INTO t VALUES (1, 'q'), (2, 'r')")
        self.db.run("INSERT INTO t (s) VALUES ('new')")
        self.assertEqual(self.rows("SELECT id, s FROM t ORDER BY id"),
                         [(1, "q"), (2, "r"), (3, "new")])

    def test_vacuum_preserves_an_integer_primary_key(self):
        # The key is data, not position: SQLite's VACUUM only renumbers rowids that are not
        # visible. Renumbering these made the alias column disagree with its own rowid.
        self.db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT)")
        self.db.run("INSERT INTO t VALUES (1, 'q'), (2, 'r')")
        self.db.run("DELETE FROM t WHERE id = 1")
        self.db.run("VACUUM")
        self.assertEqual(self.rows("SELECT id, s FROM t"), [(2, "r")])
        self.db.run("INSERT INTO t (s) VALUES ('small')")
        self.assertEqual(self.rows("SELECT id FROM t ORDER BY id"), [(2,), (3,)])

    def test_null_into_a_primary_key_follows_the_reference(self):
        # SQLite does not enforce NOT NULL on a PRIMARY KEY in a rowid table: INTEGER PRIMARY KEY
        # turns NULL into an assigned key, a TEXT PRIMARY KEY keeps the NULL.
        self.db.run("CREATE TABLE a (id INTEGER PRIMARY KEY)")
        self.db.run("INSERT INTO a VALUES (NULL)")
        self.assertEqual(self.rows("SELECT id FROM a"), [(1,)])
        self.db.run("CREATE TABLE b (k TEXT PRIMARY KEY)")
        self.db.run("INSERT INTO b VALUES (NULL)")
        self.assertEqual(self.rows("SELECT quote(k) FROM b"), [("NULL",)])


if __name__ == "__main__":
    unittest.main()
