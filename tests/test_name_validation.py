"""Planning-time name validation: a name error is decided by the schema, not the data.

The gap (STATE.md's Gap 2) was that resolution happened while rows were evaluated, so an invalid
query over an empty relation answered instead of erroring, and an unknown name inside an empty
subquery became NULL. The resolver now runs before any row is read.

The fail-open cases matter as much as the rejections: a source whose columns cannot be enumerated
statically (a view, a CTE, a table function) skips validation rather than guessing, because a
resolver that rejects valid queries is the failure class this item named as worse than the gap.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database, ExecError  # noqa: E402


class NameValidationTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def error_from(self, statement):
        with self.assertRaises(ExecError) as caught:
            self.db.run(statement)
        return str(caught.exception)

    def test_an_unknown_column_fails_on_an_empty_table(self):
        self.db.run("CREATE TABLE a (k INTEGER)")
        self.assertEqual(self.error_from("SELECT nosuch FROM a"), "no such column: nosuch")

    def test_an_unknown_qualified_column_fails_on_an_empty_table(self):
        self.db.run("CREATE TABLE t (k INTEGER)")
        self.assertEqual(self.error_from("SELECT t.nosuch FROM t"), "no such column: t.nosuch")

    def test_an_unknown_name_inside_an_empty_subquery_fails(self):
        self.db.run("CREATE TABLE a (k INTEGER)")
        self.db.run("INSERT INTO a VALUES (1)")
        self.db.run("CREATE TABLE c (v INTEGER)")
        self.assertEqual(self.error_from("SELECT a.k, (SELECT MAX(b.v) FROM c) FROM a"),
                         "no such column: b.v")

    def test_an_unknown_column_in_a_derived_scope_fails(self):
        self.db.run("CREATE TABLE t (k INTEGER)")
        self.assertEqual(self.error_from("SELECT d.nosuch FROM (SELECT k FROM t) AS d"),
                         "no such column: d.nosuch")

    def test_a_name_in_both_join_inputs_is_ambiguous(self):
        self.db.run("CREATE TABLE t (k INTEGER, v INTEGER)")
        self.db.run("CREATE TABLE u (k INTEGER, v INTEGER)")
        self.assertEqual(self.error_from("SELECT v FROM t JOIN u ON t.k = u.k"),
                         "ambiguous column name: v")

    def test_an_alias_qualifier_resolves_and_the_table_name_no_longer_does(self):
        self.db.run("CREATE TABLE t (k INTEGER)")
        self.db.run("INSERT INTO t VALUES (1)")
        self.assertEqual(self.db.run("SELECT x.k FROM t AS x")[1], [(1,)])
        self.assertEqual(self.error_from("SELECT t.k FROM t AS x"), "no such column: t.k")

    def test_the_rowid_namespace_is_not_rejected(self):
        self.db.run("CREATE TABLE t (a TEXT)")
        self.db.run("INSERT INTO t VALUES ('x')")
        self.assertEqual(self.db.run("SELECT rowid FROM t")[1], [(1,)])

    def test_a_view_source_is_validated_fail_open(self):
        self.db.run("CREATE TABLE t (k INTEGER)")
        self.db.run("INSERT INTO t VALUES (7)")
        self.db.run("CREATE VIEW v AS SELECT k FROM t")
        self.assertEqual(self.db.run("SELECT k FROM v")[1], [(7,)])

    def test_a_cte_source_is_validated_fail_open(self):
        self.db.run("CREATE TABLE t (k INTEGER)")
        self.db.run("INSERT INTO t VALUES (7)")
        self.assertEqual(self.db.run("WITH c AS (SELECT k FROM t) SELECT k FROM c")[1], [(7,)])

    def test_a_table_function_source_is_validated_fail_open(self):
        self.assertEqual(self.db.run("SELECT value FROM json_each('[10,20]') ORDER BY value")[1],
                         [(10,), (20,)])


if __name__ == "__main__":
    unittest.main()
