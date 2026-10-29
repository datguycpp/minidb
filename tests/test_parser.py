"""Parser tests for milestone 1: CREATE TABLE, INSERT, SELECT with WHERE."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.sql import ast
from minidb.sql.parser import ParseError, parse


class TestCreateTable(unittest.TestCase):
    def test_minimal(self):
        stmt = parse("CREATE TABLE t (id INTEGER)")
        self.assertEqual(stmt, ast.CreateTable("t", [ast.ColumnDef("id", "INTEGER")]))

    def test_multiple_columns_and_constraints(self):
        stmt = parse("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
        self.assertEqual([c.name for c in stmt.columns], ["id", "name"])
        self.assertEqual(stmt.columns[0].constraints, [ast.Constraint("PRIMARY KEY")])
        self.assertEqual(stmt.columns[1].constraints, [ast.Constraint("NOT NULL"), ast.Constraint("UNIQUE")])

    def test_if_not_exists(self):
        stmt = parse("CREATE TABLE IF NOT EXISTS t (id INT)")
        self.assertTrue(stmt.if_not_exists)

    def test_type_arguments(self):
        stmt = parse("CREATE TABLE t (a VARCHAR(20), b DECIMAL(10,2))")
        self.assertEqual(stmt.columns[0].type_args, [20])
        self.assertEqual(stmt.columns[1].type_args, [10, 2])

    def test_default_and_check_constraints(self):
        # Column-level only: table-level CHECK is milestone 16, not milestone 1.
        stmt = parse("CREATE TABLE t (n INTEGER DEFAULT 0 CHECK (n > -1))")
        kinds = [c.kind for c in stmt.columns[0].constraints]
        self.assertEqual(kinds, ["DEFAULT", "CHECK"])

    def test_table_level_check_is_rejected_for_now(self):
        with self.assertRaises(ParseError):
            parse("CREATE TABLE t (n INTEGER, CHECK (n > 0))")

    def test_case_insensitive_keywords(self):
        self.assertIsNotNone(parse("create table t (id integer)"))

    def test_no_columns_is_an_error(self):
        with self.assertRaises(ParseError) as ctx:
            parse("CREATE TABLE t ()")
        self.assertIn("at least one column", ctx.exception.detail)

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse("CREATE TABLE t (id WIDGET)")
        self.assertIn("unknown type", ctx.exception.detail)

    def test_only_create_table_is_accepted(self):
        with self.assertRaises(ParseError) as ctx:
            parse("CREATE INDEX i ON t (id)")
        self.assertIn("CREATE TABLE", ctx.exception.detail)


class TestInsert(unittest.TestCase):
    def test_with_column_list(self):
        stmt = parse("INSERT INTO t (a, b) VALUES (1, 'x')")
        self.assertEqual(stmt.table, "t")
        self.assertEqual(stmt.columns, ["a", "b"])
        self.assertEqual(stmt.rows, [[ast.Literal(1, "number"), ast.Literal("x", "string")]])

    def test_without_column_list(self):
        stmt = parse("INSERT INTO t VALUES (1, NULL)")
        self.assertIsNone(stmt.columns)
        self.assertEqual(stmt.rows[0][1], ast.Literal(None, "null"))

    def test_multi_row(self):
        stmt = parse("INSERT INTO t VALUES (1), (2), (3)")
        self.assertEqual(len(stmt.rows), 3)

    def test_negative_number(self):
        stmt = parse("INSERT INTO t VALUES (-5)")
        self.assertEqual(stmt.rows[0][0], ast.UnaryOp("-", ast.Literal(5, "number")))

    def test_missing_values_keyword(self):
        with self.assertRaises(ParseError) as ctx:
            parse("INSERT INTO t (a) (1)")
        self.assertIn("expected VALUES", ctx.exception.detail)


class TestSelect(unittest.TestCase):
    def test_star(self):
        stmt = parse("SELECT * FROM t")
        self.assertEqual(stmt.items, [ast.Star()])
        self.assertIsNone(stmt.where)

    def test_columns_with_alias(self):
        stmt = parse("SELECT a AS x, b FROM t")
        self.assertEqual(stmt.items[0], ast.SelectItem(ast.ColumnRef("a"), "x"))
        self.assertEqual(stmt.items[1].alias, None)

    def test_implicit_alias(self):
        stmt = parse("SELECT total t FROM sales")
        self.assertEqual(stmt.items[0].alias, "t")

    def test_table_qualified_star(self):
        stmt = parse("SELECT t.* FROM t")
        self.assertEqual(stmt.items, [ast.Star("t")])

    def test_distinct(self):
        self.assertTrue(parse("SELECT DISTINCT a FROM t").distinct)

    def test_where_comparison(self):
        stmt = parse("SELECT * FROM t WHERE id = 1")
        self.assertEqual(stmt.where, ast.BinaryOp("=", ast.ColumnRef("id"), ast.Literal(1, "number")))

    def test_double_equals_normalizes(self):
        self.assertEqual(parse("SELECT * FROM t WHERE a == 1").where.op, "=")

    def test_diamond_normalizes_to_not_equals(self):
        self.assertEqual(parse("SELECT * FROM t WHERE a <> 1").where.op, "!=")

    def test_and_binds_tighter_than_or(self):
        where = parse("SELECT * FROM t WHERE a = 1 AND b = 2 OR c = 3").where
        self.assertEqual(where.op, "OR")
        self.assertEqual(where.left.op, "AND")

    def test_parentheses_override_precedence(self):
        where = parse("SELECT * FROM t WHERE a = 1 AND (b = 2 OR c = 3)").where
        self.assertEqual(where.op, "AND")
        self.assertEqual(where.right.op, "OR")

    def test_not_applies_to_comparison(self):
        where = parse("SELECT * FROM t WHERE NOT a = 1").where
        self.assertEqual(where, ast.UnaryOp("NOT", ast.BinaryOp("=", ast.ColumnRef("a"), ast.Literal(1, "number"))))

    def test_is_null(self):
        where = parse("SELECT * FROM t WHERE a IS NULL").where
        self.assertEqual(where, ast.IsNull(ast.ColumnRef("a"), negated=False))

    def test_is_not_null(self):
        where = parse("SELECT * FROM t WHERE a IS NOT NULL").where
        self.assertEqual(where.negated, True)

    def test_boolean_literals(self):
        where = parse("SELECT * FROM t WHERE a = TRUE").where
        self.assertEqual(where.right, ast.Literal(True, "bool"))

    def test_qualified_column_in_where(self):
        where = parse("SELECT * FROM t WHERE t.a = 1").where
        self.assertEqual(where.left, ast.ColumnRef("a", table="t"))

    def test_missing_from(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT 1")
        self.assertIn("expected FROM", ctx.exception.detail)

    def test_reserved_word_cannot_be_a_table_name(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT * FROM where")
        self.assertIn("expected table name", ctx.exception.detail)


class TestErrorWording(unittest.TestCase):
    """The message text is part of the conformance contract (spec: byte-identical after the
    CLI framing strip), so it is asserted directly rather than through a substring."""

    def test_pointed_at_a_bad_token(self):
        with self.assertRaises(ParseError) as ctx:
            parse("CREAT TABLE t (a INT)")
        self.assertEqual(str(ctx.exception), 'near "CREAT": syntax error')

    def test_missing_keyword_names_that_token(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT * FRM t")
        self.assertEqual(str(ctx.exception), 'near "FRM": syntax error')

    def test_end_of_input_is_incomplete_input(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT * FROM")
        self.assertEqual(str(ctx.exception), "incomplete input")

    def test_our_diagnostic_survives_in_detail(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT * FRM t")
        self.assertIn("expected FROM", ctx.exception.detail)


class TestStatementFraming(unittest.TestCase):
    def test_optional_semicolon(self):
        for sql in ("SELECT * FROM t", "SELECT * FROM t;", "  SELECT * FROM t ;  "):
            self.assertIsNotNone(parse(sql))

    def test_trailing_garbage_is_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT * FROM t WHERE 1 = 1 extra")
        self.assertIn("trailing input", ctx.exception.detail)

    def test_empty_input(self):
        with self.assertRaises(ParseError) as ctx:
            parse("   ")
        self.assertIn("expected CREATE, INSERT or SELECT", ctx.exception.detail)

    def test_unknown_statement(self):
        with self.assertRaises(ParseError):
            parse("DROP TABLE t")

    def test_error_reports_position(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT * FROM t WHERE")
        self.assertGreaterEqual(ctx.exception.col, 20)

    def test_lexer_error_surfaces_as_parse_error(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT 'unterminated FROM t")
        self.assertIn("unterminated string", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
