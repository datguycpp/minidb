"""The sqlite_master autoindex surface (M38 follow-up, ordered by @hermes).

The reference shows `sqlite_autoindex_<table>_<n>` exactly where a constraint is not the rowid:
column-level UNIQUE for any type, column-level PRIMARY KEY unless the declared type is exactly
INTEGER, and table-level constraints unless the PRIMARY KEY is a single INTEGER column. Names and
ordering are visible in sqlite_master, so the corpus pins them (tests/corpus/t3d.txt); these tests
pin the engine-side consequences: uniqueness is still enforced where no index is created, and a
column-level TEXT key gets the index the planner then uses.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.engine import Database, ExecError  # noqa: E402


def master(db):
    return [name for kind, name in db.run("SELECT type, name FROM sqlite_master ORDER BY type, name")[1]]


class TestNames(unittest.TestCase):
    SHAPES = [
        ("CREATE TABLE a (id INTEGER PRIMARY KEY, v TEXT)", ["a"]),
        # ORDER BY type, name puts 'index' before 'table'.
        ("CREATE TABLE b (id INT PRIMARY KEY, v TEXT)", ["sqlite_autoindex_b_1", "b"]),
        ("CREATE TABLE c (k TEXT PRIMARY KEY, v TEXT)", ["sqlite_autoindex_c_1", "c"]),
        ("CREATE TABLE d (k TEXT UNIQUE, v TEXT)", ["sqlite_autoindex_d_1", "d"]),
        ("CREATE TABLE e (k TEXT UNIQUE COLLATE NOCASE)", ["sqlite_autoindex_e_1", "e"]),
        ("CREATE TABLE h (x INTEGER, y INTEGER, PRIMARY KEY (x))", ["h"]),
        ("CREATE TABLE i (x INTEGER, y INTEGER, UNIQUE (x, y))", ["sqlite_autoindex_i_1", "i"]),
        ("CREATE TABLE n (id INTEGER PRIMARY KEY AUTOINCREMENT)", ["n"]),
        ("CREATE TABLE f (k TEXT UNIQUE, j TEXT UNIQUE)", ["sqlite_autoindex_f_1", "sqlite_autoindex_f_2", "f"]),
    ]

    def test_shapes_match_the_reference(self):
        for sql, expected in self.SHAPES:
            with self.subTest(sql=sql):
                db = Database()
                db.run(sql)
                self.assertEqual(master(db), expected)

    def test_numbering_is_column_constraints_then_table_constraints(self):
        db = Database()
        db.run("CREATE TABLE m3 (a TEXT, b TEXT PRIMARY KEY, UNIQUE (a))")
        self.assertEqual(master(db), ["sqlite_autoindex_m3_1", "sqlite_autoindex_m3_2", "m3"])
        first, second = db.tables["m3"].indexes["sqlite_autoindex_m3_1"], db.tables["m3"].indexes["sqlite_autoindex_m3_2"]
        self.assertEqual(first.columns, [1])     # b, the column-level PRIMARY KEY
        self.assertEqual(second.columns, [0])    # a, the table-level UNIQUE


class TestEnforcement(unittest.TestCase):
    def test_the_rowid_form_still_refuses_a_duplicate(self):
        db = Database()
        db.run("CREATE TABLE h (x INTEGER, y TEXT, PRIMARY KEY (x))")
        db.run("INSERT INTO h VALUES (1, 'a')")
        with self.assertRaises(ExecError) as ctx:
            db.run("INSERT INTO h VALUES (1, 'b')")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: h.x")

    def test_the_indexed_form_refuses_a_duplicate(self):
        db = Database()
        db.run("CREATE TABLE c (k TEXT PRIMARY KEY, v TEXT)")
        db.run("INSERT INTO c VALUES ('a', 1)")
        with self.assertRaises(ExecError) as ctx:
            db.run("INSERT INTO c VALUES ('a', 2)")
        self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: c.k")

    def test_nocase_column_uniqueness_is_folded(self):
        db = Database()
        db.run("CREATE TABLE e (k TEXT UNIQUE COLLATE NOCASE)")
        db.run("INSERT INTO e VALUES ('Alpha')")
        with self.assertRaises(ExecError):
            db.run("INSERT INTO e VALUES ('alpha')")


class TestReopen(unittest.TestCase):
    def test_the_rowid_form_still_refuses_a_duplicate_after_reload(self):
        path = os.path.join(tempfile.mkdtemp(), "ai.db")
        db = Database(path)
        db.run("CREATE TABLE h (x INTEGER, y TEXT, PRIMARY KEY (x))")
        db.run("INSERT INTO h VALUES (1, 'a')")
        db.close()
        db = Database(path)
        try:
            with self.assertRaises(ExecError) as ctx:
                db.run("INSERT INTO h VALUES (1, 'b')")
            self.assertEqual(str(ctx.exception), "UNIQUE constraint failed: h.x")
        finally:
            db.close()

    def test_a_text_key_index_survives_reload(self):
        path = os.path.join(tempfile.mkdtemp(), "ai2.db")
        db = Database(path)
        db.run("CREATE TABLE c (k TEXT PRIMARY KEY, v TEXT)")
        for i in range(200):
            db.run(f"INSERT INTO c VALUES ('k{i:03d}', 'v{i}')")
        db.close()
        db = Database(path)
        try:
            db.run("SELECT v FROM c WHERE k = 'k050'")
            self.assertEqual(db.last_plan["access"], "index")
            self.assertEqual(db.run("SELECT v FROM c WHERE k = 'k050'")[1], [("v50",)])
        finally:
            db.close()


class TestPlannerSeesIt(unittest.TestCase):
    def test_a_column_level_text_key_is_an_index_lookup(self):
        db = Database()
        db.run("CREATE TABLE c (k TEXT PRIMARY KEY, v TEXT)")
        for i in range(200):
            db.run(f"INSERT INTO c VALUES ('k{i:03d}', 'v{i}')")
        db.run("SELECT v FROM c WHERE k = 'k050'")
        self.assertEqual(db.last_plan["access"], "index")
        self.assertEqual(db.run("SELECT v FROM c WHERE k = 'k050'")[1], [("v50",)])

    def test_an_integer_rowid_key_is_a_rowid_lookup_not_a_scan(self):
        # This assertion has now been correct twice in opposite directions, and the header says so
        # deliberately: at M38 it pinned a *scan* because the aliasing item had not landed (the
        # named remaining gap); the item landed, so the same query must now take the rowid path.
        # What it pins is unchanged -- the plan for the rowid key -- only the expected answer is.
        db = Database()
        db.run("CREATE TABLE a (id INTEGER PRIMARY KEY, v TEXT)")
        db.run("INSERT INTO a VALUES (1, 'x')")
        db.run("SELECT v FROM a WHERE id = 1")
        self.assertEqual(db.last_plan["access"], "rowid")
        self.assertEqual(db.last_plan["reason"], "rowid-equality")


if __name__ == "__main__":
    unittest.main()
