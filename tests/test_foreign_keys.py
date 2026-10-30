"""Milestone 16 part 2: PRAGMA foreign_keys and REFERENCES enforcement."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402

SEED = [
    "CREATE TABLE p (id INTEGER PRIMARY KEY)",
    "CREATE TABLE c (id INTEGER, pid INTEGER REFERENCES p(id))",
    "INSERT INTO p VALUES (1)",
]


class TestForeignKeys(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SEED:
            self.db.run(statement)

    def test_enforcement_is_off_by_default(self):
        self.db.run("INSERT INTO c VALUES (10, 999)")
        self.assertEqual(self.db.run("SELECT count(*) FROM c")[1], [(1,)],
                         "SQLite does not enforce foreign keys unless asked")

    def test_pragma_reads_back_zero(self):
        self.assertEqual(self.db.run("PRAGMA foreign_keys")[1], [(0,)])

    def test_pragma_enables_enforcement(self):
        self.db.run("PRAGMA foreign_keys = ON")
        self.assertEqual(self.db.run("PRAGMA foreign_keys")[1], [(1,)])
        with self.assertRaises(ExecError) as ctx:
            self.db.run("INSERT INTO c VALUES (11, 999)")
        self.assertEqual(str(ctx.exception), "FOREIGN KEY constraint failed")

    def test_valid_child_still_inserts(self):
        self.db.run("PRAGMA foreign_keys = ON")
        self.db.run("INSERT INTO c VALUES (12, 1)")
        self.assertEqual(self.db.run("SELECT count(*) FROM c")[1], [(1,)])

    def test_null_child_value_is_exempt(self):
        self.db.run("PRAGMA foreign_keys = ON")
        self.db.run("INSERT INTO c VALUES (13, NULL)")
        self.assertEqual(self.db.run("SELECT count(*) FROM c")[1], [(1,)])

    def test_deleting_a_referenced_parent_is_refused(self):
        self.db.run("PRAGMA foreign_keys = ON")
        self.db.run("INSERT INTO c VALUES (14, 1)")
        with self.assertRaises(ExecError) as ctx:
            self.db.run("DELETE FROM p WHERE id = 1")
        self.assertEqual(str(ctx.exception), "FOREIGN KEY constraint failed")


class DifferentialForeignKeys(unittest.TestCase):
    CASES = [
        SEED + ["INSERT INTO c VALUES (10, 999)", "SELECT count(*) FROM c"],
        SEED + ["PRAGMA foreign_keys"],
        SEED + ["PRAGMA foreign_keys = ON", "INSERT INTO c VALUES (11, 999)"],
        SEED + ["PRAGMA foreign_keys = ON", "INSERT INTO c VALUES (12, 1)", "SELECT count(*) FROM c"],
        SEED + ["PRAGMA foreign_keys = ON", "INSERT INTO c VALUES (13, NULL)",
                "SELECT count(*) FROM c"],
        SEED + ["PRAGMA foreign_keys = ON", "INSERT INTO c VALUES (14, 1)", "DELETE FROM p WHERE id = 1"],
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
