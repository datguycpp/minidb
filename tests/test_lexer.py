"""Lexer tests: the token stream is the parser's whole world, so it gets its own coverage."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.sql.lexer import LexError, tokenize


def kinds(sql):
    return [(t.kind, t.value) for t in tokenize(sql) if t.kind != "EOF"]


class TestLexer(unittest.TestCase):
    def test_keywords_are_case_insensitive_but_keep_source_text(self):
        toks = tokenize("SeLeCt")
        self.assertEqual(toks[0].kind, "KEYWORD")
        self.assertEqual(toks[0].value, "SeLeCt")
        self.assertEqual(toks[0].upper, "SELECT")

    def test_identifier_with_digits_and_underscore(self):
        self.assertEqual(kinds("user_2"), [("IDENT", "user_2")])

    def test_single_quoted_string_doubles_escape(self):
        self.assertEqual(kinds("'it''s'"), [("STRING", "it's")])

    def test_double_quoted_identifier(self):
        self.assertEqual(kinds('"select"'), [("IDENT", "select")])

    def test_bracket_and_backtick_identifiers(self):
        self.assertEqual(kinds("[odd name]"), [("IDENT", "odd name")])
        self.assertEqual(kinds("`x`"), [("IDENT", "x")])

    def test_numbers(self):
        self.assertEqual(kinds("42"), [("NUMBER", "42")])
        self.assertEqual(kinds("3.5"), [("NUMBER", "3.5")])
        self.assertEqual(kinds("1e3"), [("NUMBER", "1e3")])
        self.assertEqual(kinds(".5"), [("NUMBER", ".5")])

    def test_line_comment_is_skipped(self):
        self.assertEqual(kinds("1 -- comment here\n2"), [("NUMBER", "1"), ("NUMBER", "2")])

    def test_block_comment_is_skipped(self):
        self.assertEqual(kinds("1 /* a /* not nested */ 2"), [("NUMBER", "1"), ("NUMBER", "2")])

    def test_multi_char_operators_win_over_single(self):
        self.assertEqual(kinds("a<=b"), [("IDENT", "a"), ("PUNCT", "<="), ("IDENT", "b")])
        self.assertEqual(kinds("a<>b"), [("IDENT", "a"), ("PUNCT", "<>"), ("IDENT", "b")])

    def test_positions_are_tracked_across_lines(self):
        toks = tokenize("a\n  b")
        self.assertEqual((toks[1].line, toks[1].col), (2, 3))

    def test_unterminated_string_reports_position(self):
        with self.assertRaises(LexError) as ctx:
            tokenize("SELECT 'oops")
        self.assertIn("unterminated string", str(ctx.exception))
        self.assertEqual(ctx.exception.col, 8)

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexError):
            tokenize("/* never closed")

    def test_unexpected_character(self):
        with self.assertRaises(LexError) as ctx:
            tokenize("SELECT #")
        self.assertIn("unexpected character", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
