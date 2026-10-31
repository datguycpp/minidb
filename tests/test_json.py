"""Milestone 35 tests: JSON functions and the -> / ->> operators.

Every case is checked against the pinned binary through the same normalizer the corpus uses; the
unit assertions below exist to state the rules in one place (and to fail with a readable message
when a rule changes), not to replace the differential.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402

DOC = '{"a":{"b":[10,20]},"s":"x","n":1.5,"t":true,"z":null}'

CASES = [
    f"SELECT json('{DOC}')",
    "SELECT json('  { \"a\" : 1 }  ')",
    f"SELECT json_extract('{DOC}', '$.a.b[1]')",
    f"SELECT json_extract('{DOC}', '$.z') IS NULL",
    f"SELECT json_extract('{DOC}', '$.missing') IS NULL",
    f"SELECT typeof(json_extract('{DOC}', '$.n'))",
    f"SELECT json_type('{DOC}'), json_type('$.a') IS NULL",
    "SELECT json_type('[1]'), json_type('\"x\"'), json_type('1'), json_type('null'), json_type('true')",
    "SELECT json_valid('{\"a\":1}'), json_valid('nope'), ifnull(json_valid(NULL), 'NULL')",
    "SELECT json_array(1, 'x', NULL, 2.5)",
    "SELECT json_object('a', 1, 'b', 'x')",
    "SELECT json_quote('it''s'), json_quote(5), json_quote(NULL)",
    "SELECT json_array_length('[1,2,3]'), json_array_length('{\"a\":1}')",
    f"SELECT '{DOC}' -> '$.a'",
    f"SELECT '{DOC}' ->> '$.a.b[0]'",
    f"SELECT '{DOC}' ->> '$.s', typeof('{DOC}' ->> '$.s')",
    f"SELECT '{DOC}' ->> '$.n', typeof('{DOC}' ->> '$.n')",
    f"SELECT '{DOC}' ->> '$.t', typeof('{DOC}' ->> '$.t')",
    f"SELECT '{DOC}' ->> '$.z' IS NULL",
    "SELECT 2 * '{\"a\":3}' ->> '$.a'",
    "SELECT 'a' || 1 * 2",
    "SELECT 1 + 2 || 'x'",
    "SELECT json_extract('[1,2,3]', '$[#]')",
    "SELECT json_extract('{\"a\":1}', '$.\"a\"') IS NULL",
]

ERRORS = [
    "SELECT json('nope')",
    "SELECT json_extract('{\"a\":1}', '$.')",
    "SELECT json_extract('{\"a\":1}', 'a')",
]


class TestJsonDifferential(unittest.TestCase):
    def test_cases_match_the_binary(self):
        for sql in CASES:
            with self.subTest(sql=sql):
                expected = bc.run_sqlite([sql])
                actual = bc.run_minidb([sql])
                ok, detail = bc.compare(expected, actual)
                self.assertTrue(ok, f"{sql} -> {detail}")

    def test_error_text_matches_the_binary(self):
        for sql in ERRORS:
            with self.subTest(sql=sql):
                expected = bc.run_sqlite([sql])
                actual = bc.run_minidb([sql])
                ok, detail = bc.compare(expected, actual)
                self.assertTrue(ok, f"{sql} -> {detail}")


class TestJsonRules(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def run_one(self, sql):
        return self.db.run(sql)[1]

    def test_arrow_is_text_and_arrow_arrow_is_a_value(self):
        self.assertEqual(self.run_one("SELECT '{\"a\":5}' -> '$.a'"), [("5",)])
        self.assertEqual(self.run_one("SELECT '{\"a\":5}' ->> '$.a'"), [(5,)])

    def test_json_normalises_whitespace_but_keeps_key_order(self):
        self.assertEqual(self.run_one("SELECT json('  { \"b\" : 2 , \"a\" : 1 }  ')"),
                         [('{"b":2,"a":1}',)])

    def test_missing_path_is_null_and_bad_path_is_an_error(self):
        self.assertEqual(self.run_one("SELECT json_extract('{\"a\":1}', '$.zz')"), [(None,)])
        with self.assertRaises(ExecError) as caught:
            self.run_one("SELECT json_extract('{\"a\":1}', '$.')")
        self.assertEqual(str(caught.exception), "bad JSON path: '$.'")


if __name__ == "__main__":
    unittest.main()
