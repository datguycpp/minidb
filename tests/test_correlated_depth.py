"""Correlated names resolve at any nesting depth, not just one level up.

The growth run's first finding: every double-nested generated case failed with
`no such column: a.k1`, because the engine kept only the immediate parent's row for
correlation. SQLite resolves the name wherever it lives in the enclosing chain, so the
fix keeps a stack of scopes (innermost last) and searches it outward. These tests pin the
resolution rule itself -- including the two ways it could go wrong in the other direction:
binding an inner column to an outer row of the same name, and silently accepting a name
that does not exist anywhere.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402


def rows(db, statement):
    return db.run(statement)[1]


SETUP = [
    "CREATE TABLE a (id INTEGER, k1 INTEGER)",
    "CREATE TABLE b (v INTEGER)",
    "CREATE TABLE c (k1 INTEGER)",
    "INSERT INTO a VALUES (1, 10), (2, 20)",
    "INSERT INTO b VALUES (10), (30)",
    "INSERT INTO c VALUES (10), (20)",
]


class CorrelatedDepthTest(unittest.TestCase):
    def build(self):
        db = Database()
        for statement in SETUP:
            db.run(statement)
        return db

    def test_depth_two_in_subquery_sees_the_outer_row(self):
        db = self.build()
        answer = rows(db, "SELECT a.id, (SELECT COUNT(*) FROM b WHERE b.v IN "
                          "(SELECT c.k1 FROM c WHERE c.k1 = a.k1)) FROM a ORDER BY a.id")
        # a.k1=20 finds c.k1=20, but no b.v equals it: 0, not 1.
        self.assertEqual(answer, [(1, 1), (2, 0)])

    def test_depth_two_exists_sees_the_outer_row(self):
        db = self.build()
        answer = rows(db, "SELECT a.id FROM a WHERE EXISTS (SELECT 1 FROM b WHERE EXISTS "
                          "(SELECT 1 FROM c WHERE c.k1 = a.k1)) ORDER BY a.id")
        self.assertEqual(answer, [(1,), (2,)])

    def test_depth_three_walks_the_whole_chain(self):
        db = self.build()
        answer = rows(db, "SELECT a.id, (SELECT COUNT(*) FROM b WHERE b.v IN "
                          "(SELECT c.k1 FROM c WHERE c.k1 IN "
                          "(SELECT b.v FROM b WHERE b.v = a.k1))) FROM a ORDER BY a.id")
        # The chain resolves (no error), and a.k1=20 dies at the innermost level: no b.v=20.
        self.assertEqual(answer, [(1, 1), (2, 0)])

    def test_an_inner_column_of_the_same_name_wins(self):
        db = Database()
        for statement in ("CREATE TABLE a (id INTEGER, k1 INTEGER)",
                          "CREATE TABLE c (k1 INTEGER)",
                          "INSERT INTO a VALUES (1, 10), (2, 20)",
                          "INSERT INTO c VALUES (10), (99)"):
            db.run(statement)
        # c has its own k1, so the aggregate is over c rows matching c.k1 = a.k1, not over a.
        answer = rows(db, "SELECT a.id, (SELECT MAX(c.k1) FROM c WHERE c.k1 = a.k1) "
                          "FROM a ORDER BY a.id")
        self.assertEqual(answer, [(1, 10), (2, None)])

    def test_a_name_that_exists_nowhere_still_raises(self):
        db = self.build()
        with self.assertRaises(Exception) as caught:
            db.run("SELECT a.id, (SELECT COUNT(*) FROM b WHERE b.v IN "
                   "(SELECT c.k1 FROM c WHERE c.k1 = a.zz)) FROM a")
        self.assertIn("no such column", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
