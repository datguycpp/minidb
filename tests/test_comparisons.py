"""Comparison semantics: collation extent, affinity, and the ORDER BY alias.

Skeptic round 1's findings, fixed and pinned here so the corpus can adopt them later without the
behaviour resting on a probe file. Each test names the case it came from.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def rows(self, statement):
        return self.db.run(statement)[1]

    def test_nocase_folds_ascii_only(self):
        # Case 16: NOCASE is SQLite's ASCII fold, so 'Ä' and 'ä' are different strings.
        self.db.run("CREATE TABLE t (s TEXT COLLATE NOCASE)")
        self.db.run("INSERT INTO t VALUES ('Ä'), ('ä'), ('A'), ('a')")
        self.assertEqual(self.rows("SELECT s FROM t WHERE s = 'Ä' ORDER BY s COLLATE BINARY"),
                         [("Ä",)])
        self.assertEqual(self.rows("SELECT count(*) FROM t WHERE s = 'a'"), [(2,)])

    def test_nocase_applies_in_a_join_on(self):
        # Case 21: a join has no single table for the collation to come from.
        self.db.run("CREATE TABLE t (a TEXT COLLATE NOCASE)")
        self.db.run("CREATE TABLE u (a TEXT COLLATE NOCASE)")
        self.db.run("INSERT INTO t VALUES ('Foo')")
        self.db.run("INSERT INTO u VALUES ('foo')")
        self.assertEqual(self.rows("SELECT t.a AS ta, u.a AS ua FROM t JOIN u ON t.a = u.a"),
                         [("Foo", "foo")])

    def test_column_affinity_applies_to_the_compared_literal(self):
        # Cases 19 and 20: `n = '1'` matches integer 1, and `s = 1` matches the text '1'.
        self.db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER, s TEXT)")
        self.db.run("INSERT INTO t VALUES (1, 1, '1'), (2, 2, 'abc')")
        self.assertEqual(self.rows("SELECT id FROM t WHERE n = '1' ORDER BY id"), [(1,)])
        self.assertEqual(self.rows("SELECT id FROM t WHERE s = 1 ORDER BY id"), [(1,)])

    def test_order_by_an_output_alias(self):
        # Case 22: ORDER BY names the projection's alias, not a table column.
        self.db.run("CREATE TABLE t (v)")
        self.db.run("CREATE TABLE u (v)")
        self.db.run("INSERT INTO t VALUES (X'41'), ('A')")
        self.db.run("INSERT INTO u VALUES ('A')")
        self.assertEqual(
            self.rows("SELECT typeof(t.v) AS ty FROM t JOIN u ON t.v = u.v ORDER BY ty"),
            [("text",)])

    def test_zeroblob(self):
        # Case 25: was missing entirely.
        self.db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, b BLOB)")
        self.db.run("INSERT INTO t VALUES (1, zeroblob(10000))")
        self.assertEqual(self.rows("SELECT length(b) FROM t"), [(10000,)])
        self.assertEqual(self.rows("SELECT hex(substr(b, 1, 2)) FROM t"), [("0000",)])


if __name__ == "__main__":
    unittest.main()
