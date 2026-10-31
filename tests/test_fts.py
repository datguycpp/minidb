"""Milestone 34 tests: full-text search over the declared fts5 subset.

Declared subset: plain terms, AND / OR / NOT, column filters (`title:dog`), trailing prefix
(`qui*`), unicode61-style case folding. Declared OUT of subset: bm25() ranking, NEAR,
tokenizer options, phrase queries. Those are named here so silence cannot be mistaken for support.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402

SETUP = ["CREATE VIRTUAL TABLE docs USING fts5(body)",
         "INSERT INTO docs VALUES ('the quick brown fox')",
         "INSERT INTO docs VALUES ('a lazy dog sleeps')"]

CASES = [
    SETUP + ["SELECT body FROM docs WHERE docs MATCH 'fox'"],
    SETUP + ["SELECT body FROM docs WHERE docs MATCH 'FOX'"],
    SETUP + ["SELECT body FROM docs WHERE docs MATCH 'qui*'"],
    SETUP + ["SELECT body FROM docs WHERE docs MATCH 'zebra'"],
    SETUP + ["SELECT count(*) FROM docs WHERE docs MATCH 'quick AND brown'"],
    SETUP + ["SELECT body FROM docs WHERE docs MATCH 'quick OR lazy' ORDER BY body"],
    SETUP + ["SELECT body FROM docs WHERE docs MATCH 'quick NOT lazy'"],
    ["CREATE VIRTUAL TABLE docs USING fts5(title, body)",
     "INSERT INTO docs VALUES ('fox hunting', 'an article about dogs')",
     "INSERT INTO docs VALUES ('dog training', 'an article about foxes')",
     "SELECT body FROM docs WHERE docs MATCH 'title:dog'"],
    ["CREATE VIRTUAL TABLE docs USING fts5(title, body)",
     "INSERT INTO docs VALUES ('fox hunting', 'an article about dogs')",
     "INSERT INTO docs VALUES ('dog training', 'an article about foxes')",
     "SELECT title FROM docs WHERE docs MATCH 'body:foxes'"],
]


class TestFtsDifferential(unittest.TestCase):
    def test_declared_subset_matches_the_binary(self):
        for statements in CASES:
            with self.subTest(sql=statements[-1]):
                expected = bc.run_sqlite([statements])
                actual = bc.run_minidb([statements])
                ok, detail = bc.compare(expected, actual)
                self.assertTrue(ok, f"{statements[-1]} -> {detail}")


class TestFtsRules(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for statement in SETUP:
            self.db.run(statement)

    def rows(self, sql):
        return self.db.run(sql)[1]

    def test_match_is_case_folded(self):
        self.assertEqual(self.rows("SELECT body FROM docs WHERE docs MATCH 'FOX'"),
                         [("the quick brown fox",)])

    def test_prefix_requires_the_star(self):
        self.assertEqual(len(self.rows("SELECT body FROM docs WHERE docs MATCH 'qui*'")), 1)
        self.assertEqual(self.rows("SELECT body FROM docs WHERE docs MATCH 'qui'"), [])

    def test_no_match_is_empty_not_an_error(self):
        self.assertEqual(self.rows("SELECT body FROM docs WHERE docs MATCH 'zebra'"), [])

    def test_not_excludes_rows_containing_the_term(self):
        self.assertEqual(self.rows("SELECT body FROM docs WHERE docs MATCH 'lazy NOT dog'"), [])

    def test_unknown_module_is_refused(self):
        from minidb.engine import ExecError
        with self.assertRaises(ExecError):
            self.db.run("CREATE VIRTUAL TABLE x USING nosuchmodule(a)")


if __name__ == "__main__":
    unittest.main()
