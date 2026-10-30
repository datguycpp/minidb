"""Recursive-descent parser for the MiniDB SQL subset (milestone 1).

Scope is deliberately narrow — CREATE TABLE, INSERT, SELECT with WHERE — because milestone 10
owns the full expression engine. Anything outside the subset raises ParseError rather than
being silently accepted and mis-parsed later.
"""

from . import ast
from .lexer import LexError, tokenize

RESERVED_STARTERS = {"CREATE", "INSERT", "SELECT"}

# Words that cannot be bare identifiers in this subset. SQLite needs them quoted too, so
# accepting `FROM where` would parse a statement no real engine accepts.
RESERVED_IDENTIFIERS = RESERVED_STARTERS | {
    "BEGIN", "COMMIT", "ROLLBACK", "END", "TRANSACTION", "DEFERRED", "IMMEDIATE", "EXCLUSIVE",
    "FROM", "WHERE", "INTO", "VALUES", "TABLE", "AND", "OR", "NOT", "IS", "NULL",
    "PRIMARY", "KEY", "UNIQUE", "DEFAULT", "CHECK", "AS", "DISTINCT", "TRUE", "FALSE",
    # Join and grouping words are reserved in SQLite's grammar; treating them as identifiers
    # makes our error land on the *next* token, which is a byte-level rejection mismatch.
    # SQLite reserves JOIN/ON/GROUP/HAVING/ORDER/LIMIT but still accepts BY and INNER as bare
    # identifiers ("SELECT by FROM t" reports no such column, not a syntax error). Matching that
    # split is what keeps rejection messages byte-identical.
    "JOIN", "ON", "GROUP", "HAVING", "ORDER", "LIMIT",
}


CONSTRAINT_STARTERS = {"PRIMARY", "NOT", "NULL", "UNIQUE", "DEFAULT", "CHECK", "REFERENCES"}

COMPARISON_OPS = {"=", "==", "!=", "<>", "<", "<=", ">", ">="}

# Type names accepted in CREATE TABLE. Anything else needs quoting, same as SQLite's leniency
# being deferred until milestone 17 (type affinity).
TYPE_NAMES = {
    "INT", "INTEGER", "TINYINT", "SMALLINT", "MEDIUMINT", "BIGINT", "UNSIGNED",
    "REAL", "DOUBLE", "FLOAT", "NUMERIC", "DECIMAL",
    "TEXT", "CLOB", "CHAR", "VARCHAR", "CHARACTER", "NCHAR", "NVARCHAR",
    "BLOB", "BOOLEAN", "DATE", "DATETIME",
}


class ParseError(Exception):
    """Raises SQLite's wording, keeps our diagnostic alongside it.

    `str(exc)` is what SQLite itself would print (`near "X": syntax error`, or
    `incomplete input` at end of statement) because that text is part of the conformance
    contract. `exc.detail` keeps the specific explanation — expected token, position — for
    humans and tests. Losing our own diagnostics to match a yacc parser would be a bad trade,
    so both are kept.
    """

    def __init__(self, detail, token=None, sqlite_text=None, line=None, col=None):
        self.detail = detail
        self.token = token
        if token is not None:
            line, col = token.line, token.col
        self.line = line
        self.col = col
        if sqlite_text is None:
            sqlite_text = self._sqlite_text(token)
        super().__init__(sqlite_text)

    @staticmethod
    def _sqlite_text(token):
        if token is None or token.kind == "EOF":
            return "incomplete input"
        return f'near "{token.raw or token.value}": syntax error'



class Parser:
    def __init__(self, tokens, source=""):
        self.tokens = tokens
        self.source = source
        self.i = 0

    # -- token helpers ---------------------------------------------------------
    @property
    def tok(self):
        return self.tokens[self.i]

    def peek(self, offset=1):
        j = min(self.i + offset, len(self.tokens) - 1)
        return self.tokens[j]

    def advance(self):
        tok = self.tokens[self.i]
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def at_keyword(self, *words):
        return self.tok.kind == "KEYWORD" and self.tok.upper in words

    def at_punct(self, *chars):
        return self.tok.kind == "PUNCT" and self.tok.value in chars

    def accept_keyword(self, *words):
        if self.at_keyword(*words):
            return self.advance()
        return None

    def expect_keyword(self, word):
        if not self.at_keyword(word):
            raise ParseError(f"expected {word}, got {self.describe(self.tok)}", self.tok)
        return self.advance()

    def accept_punct(self, char):
        if self.at_punct(char):
            return self.advance()
        return None

    def expect_punct(self, char):
        if not self.at_punct(char):
            raise ParseError(f"expected {char!r}, got {self.describe(self.tok)}", self.tok)
        return self.advance()

    @staticmethod
    def describe(tok):
        if tok.kind == "EOF":
            return "end of input"
        return f"{tok.value!r}"

    def parse_identifier(self, what="identifier"):
        if self.tok.kind == "IDENT":
            return self.advance().value
        if self.tok.kind == "KEYWORD" and self.tok.upper not in RESERVED_IDENTIFIERS:
            # SQLite allows many keywords as identifiers; quoted forms cover the rest.
            return self.advance().value
        raise ParseError(f"expected {what}, got {self.describe(self.tok)}", self.tok)

    # -- entry point -----------------------------------------------------------
    def parse_statement(self):
        if self.at_keyword("CREATE"):
            stmt = self.parse_create()
        elif self.at_keyword("INSERT"):
            stmt = self.parse_insert()
        elif self.at_keyword("BEGIN"):
            stmt = self.parse_begin()
        elif self.at_keyword("COMMIT", "END"):
            stmt = self.parse_commit()
        elif self.at_keyword("ROLLBACK"):
            stmt = self.parse_rollback()
        elif self.at_keyword("SELECT"):
            stmt = self.parse_select()
        else:
            raise ParseError(f"expected CREATE, INSERT or SELECT, got {self.describe(self.tok)}", self.tok)

        self.accept_punct(";")
        if self.tok.kind != "EOF":
            raise ParseError(f"unexpected trailing input {self.describe(self.tok)}", self.tok)
        return stmt

    # -- transactions ----------------------------------------------------------
    def parse_begin(self):
        self.expect_keyword("BEGIN")
        mode = None
        if self.at_keyword("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
            mode = self.advance().upper
        self.accept_keyword("TRANSACTION")
        return ast.Begin(mode=mode)

    def parse_commit(self):
        self.advance()                      # COMMIT or END
        self.accept_keyword("TRANSACTION")
        return ast.Commit()

    def parse_rollback(self):
        self.expect_keyword("ROLLBACK")
        self.accept_keyword("TRANSACTION")
        return ast.Rollback()

    # -- CREATE TABLE ----------------------------------------------------------
    def parse_create(self):
        start = self.expect_keyword("CREATE")
        unique = False
        if self.accept_keyword("UNIQUE"):
            unique = True
            if not self.at_keyword("INDEX"):
                raise ParseError("expected INDEX after UNIQUE", self.tok)
        if self.at_keyword("INDEX"):
            self.advance()
            return self.parse_create_index(unique)
        if unique:
            raise ParseError("expected INDEX after UNIQUE", self.tok)
        if not self.at_keyword("TABLE"):
            raise ParseError("only CREATE TABLE and CREATE INDEX are supported", self.tok)
        self.advance()

        if_not_exists = False
        if self.accept_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            if_not_exists = True

        name = self.parse_identifier("table name")
        self.expect_punct("(")

        columns = []
        if self.at_punct(")"):
            raise ParseError("CREATE TABLE needs at least one column", self.tok)
        while True:
            columns.append(self.parse_column_def())
            if self.accept_punct(","):
                continue
            break
        self.expect_punct(")")
        statement = ast.CreateTable(name=name, columns=columns, if_not_exists=if_not_exists)
        # The catalog keeps the CREATE statement verbatim so reopening needs no AST serialiser.
        statement.sql = self.source.strip() if self.source else None
        return statement

    def parse_create_index(self, unique):
        if_not_exists = False
        if self.accept_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            if_not_exists = True
        name = self.parse_identifier("index name")
        self.expect_keyword("ON")
        table = self.parse_identifier("table name")
        self.expect_punct("(")
        columns = [self.parse_identifier("column name")]
        self.accept_keyword("ASC") or self.accept_keyword("DESC")
        while self.accept_punct(","):
            columns.append(self.parse_identifier("column name"))
            self.accept_keyword("ASC") or self.accept_keyword("DESC")
        self.expect_punct(")")
        statement = ast.CreateIndex(name=name, table=table, columns=columns,
                                    unique=unique, if_not_exists=if_not_exists)
        statement.sql = self.source.strip() if self.source else None
        return statement

    def parse_column_def(self):
        name = self.parse_identifier("column name")
        type_name, type_args = None, []
        if self.tok.kind == "IDENT" or (self.tok.kind == "KEYWORD" and self.tok.upper in TYPE_NAMES):
            word = self.advance().value
            if word.upper() not in TYPE_NAMES:
                raise ParseError(f"unknown type {word!r}", self.tokens[self.i - 1])
            type_name = word.upper()
            # VARCHAR(20), DECIMAL(10,2)
            if self.at_punct("("):
                self.advance()
                while True:
                    if self.tok.kind != "NUMBER":
                        raise ParseError("expected a number in type arguments", self.tok)
                    type_args.append(int(self.advance().value))
                    if self.accept_punct(","):
                        continue
                    break
                self.expect_punct(")")

        constraints = []
        while self.tok.kind == "KEYWORD" and self.tok.upper in CONSTRAINT_STARTERS:
            constraints.append(self.parse_column_constraint())
        return ast.ColumnDef(name=name, type_name=type_name, type_args=type_args, constraints=constraints)

    def parse_column_constraint(self):
        if self.accept_keyword("PRIMARY"):
            self.expect_keyword("KEY")
            return ast.Constraint("PRIMARY KEY")
        if self.accept_keyword("NOT"):
            self.expect_keyword("NULL")
            return ast.Constraint("NOT NULL")
        if self.accept_keyword("NULL"):
            return ast.Constraint("NULL")
        if self.accept_keyword("UNIQUE"):
            return ast.Constraint("UNIQUE")
        if self.accept_keyword("DEFAULT"):
            return ast.Constraint("DEFAULT", self.parse_expression())
        if self.accept_keyword("CHECK"):
            open_paren = self.expect_punct("(")
            expr = self.parse_expression()
            close_paren = self.tok
            self.expect_punct(")")
            # SQLite echoes the CHECK expression verbatim in its error message, so the source
            # span is captured here rather than reconstructed from the AST later.
            text = self.source[open_paren.pos + 1:close_paren.pos].strip()
            return ast.Constraint("CHECK", expr, text=text)
        raise ParseError("unsupported column constraint", self.tok)

    # -- INSERT ----------------------------------------------------------------
    def parse_insert(self):
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table = self.parse_identifier("table name")

        columns = None
        if self.at_punct("("):
            self.advance()
            columns = [self.parse_identifier("column name")]
            while self.accept_punct(","):
                columns.append(self.parse_identifier("column name"))
            self.expect_punct(")")

        self.expect_keyword("VALUES")
        rows = []
        while True:
            self.expect_punct("(")
            row = [self.parse_expression()]
            while self.accept_punct(","):
                row.append(self.parse_expression())
            self.expect_punct(")")
            rows.append(row)
            if not self.accept_punct(","):
                break
        return ast.Insert(table=table, columns=columns, rows=rows)

    # -- SELECT ----------------------------------------------------------------
    def parse_select(self):
        self.expect_keyword("SELECT")
        distinct = bool(self.accept_keyword("DISTINCT"))

        if self.at_punct("*"):
            self.advance()
            items = [ast.Star()]
        else:
            items = [self.parse_select_item()]
            while self.accept_punct(","):
                items.append(self.parse_select_item())

        self.expect_keyword("FROM")
        table = self.parse_identifier("table name")
        alias = self.parse_optional_alias()

        joins = []
        while self.at_keyword("JOIN") or self.at_keyword("INNER"):
            if self.at_keyword("INNER"):
                self.advance()
            self.expect_keyword("JOIN")
            join_table = self.parse_identifier("table name")
            join_alias = self.parse_optional_alias()
            on = None
            if self.accept_keyword("ON"):
                on = self.parse_expression()
            joins.append(ast.Join(table=join_table, alias=join_alias, on=on))

        where = None
        if self.accept_keyword("WHERE"):
            where = self.parse_expression()

        group_by = []
        if self.accept_keyword("GROUP"):
            self.expect_keyword("BY")
            group_by.append(self.parse_expression())
            while self.accept_punct(","):
                group_by.append(self.parse_expression())

        having = None
        if self.accept_keyword("HAVING"):
            having = self.parse_expression()

        return ast.Select(items=items, table=table, where=where, distinct=distinct,
                          alias=alias, joins=joins, group_by=group_by, having=having)

    def parse_optional_alias(self):
        if self.accept_keyword("AS"):
            return self.parse_identifier("alias")
        if self.tok.kind == "IDENT":
            return self.advance().value
        return None

    def parse_select_item(self):
        # table.* shorthand
        if self.tok.kind in ("IDENT", "KEYWORD") and self.peek().kind == "PUNCT" and self.peek().value == "." \
                and self.peek(2).kind == "PUNCT" and self.peek(2).value == "*":
            table = self.advance().value
            self.advance()  # .
            self.advance()  # *
            return ast.Star(table=table)

        start = self.tok.pos
        expr = self.parse_expression()
        last = self.tokens[self.i - 1]
        text = self.source[start:last.pos + len(last.raw)].strip() if self.source else None
        alias = None
        if self.accept_keyword("AS"):
            alias = self.parse_identifier("alias")
        elif self.tok.kind == "IDENT":
            alias = self.advance().value
        return ast.SelectItem(expr=expr, alias=alias, text=text)

    # -- expressions -----------------------------------------------------------
    def parse_expression(self):
        return self.parse_or()

    def parse_or(self):
        node = self.parse_and()
        while self.accept_keyword("OR"):
            node = ast.BinaryOp("OR", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_not()
        while self.accept_keyword("AND"):
            node = ast.BinaryOp("AND", node, self.parse_not())
        return node

    def parse_not(self):
        if self.accept_keyword("NOT"):
            return ast.UnaryOp("NOT", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        node = self.parse_primary()
        # IS [NOT] NULL
        if self.accept_keyword("IS"):
            negated = bool(self.accept_keyword("NOT"))
            self.expect_keyword("NULL")
            node = ast.IsNull(node, negated=negated)
            return node
        in_predicate = self.at_keyword("IN") or (
            self.at_keyword("NOT") and self.peek().kind == "KEYWORD" and self.peek().upper == "IN")
        if in_predicate:
            negated = False
            if self.at_keyword("NOT"):
                self.advance()
                negated = True
            self.expect_keyword("IN")
            self.expect_punct("(")
            if not self.at_keyword("SELECT"):
                raise ParseError("only IN (SELECT ...) is supported", self.tok)
            inner = self.parse_select()
            self.expect_punct(")")
            return ast.InSubquery(expr=node, select=inner, negated=negated)

        if self.tok.kind == "PUNCT" and self.tok.value in COMPARISON_OPS:
            op = self.advance().value
            if op == "==":
                op = "="
            elif op == "<>":
                op = "!="
            return ast.BinaryOp(op, node, self.parse_primary())
        return node

    def parse_primary(self):
        tok = self.tok

        if tok.kind == "PUNCT" and tok.value == "(":
            self.advance()
            if self.at_keyword("SELECT"):
                inner = self.parse_select()
                self.expect_punct(")")
                return ast.Subquery(inner)
            node = self.parse_expression()
            self.expect_punct(")")
            return node

        if tok.kind == "NUMBER":
            self.advance()
            text = tok.value
            value = float(text) if any(c in text for c in ".eE") else int(text)
            return ast.Literal(value, "number")

        if tok.kind == "STRING":
            self.advance()
            return ast.Literal(tok.value, "string")

        if tok.kind == "KEYWORD" and tok.upper == "NULL":
            self.advance()
            return ast.Literal(None, "null")

        if tok.kind == "KEYWORD" and tok.upper in ("TRUE", "FALSE"):
            self.advance()
            return ast.Literal(tok.upper == "TRUE", "bool")

        if tok.kind == "PUNCT" and tok.value in ("-", "+"):
            op = self.advance().value
            return ast.UnaryOp(op, self.parse_primary())

        if tok.kind in ("IDENT", "KEYWORD") and tok.upper in ("COUNT", "SUM", "AVG", "MIN", "MAX") \
                and self.peek().kind == "PUNCT" and self.peek().value == "(":
            name = self.advance().upper
            self.expect_punct("(")
            if self.at_punct("*"):
                self.advance()
                self.expect_punct(")")
                return ast.FuncCall(name=name, arg=None, star=True)
            argument = self.parse_expression()
            self.expect_punct(")")
            return ast.FuncCall(name=name, arg=argument)

        if tok.kind in ("IDENT", "KEYWORD") and tok.upper not in RESERVED_IDENTIFIERS:
            name = self.advance().value
            quote = tok.quote
            if self.at_punct("."):
                self.advance()
                column_tok = self.tok
                column = self.parse_identifier("column name")
                return ast.ColumnRef(name=column, table=name, quote=column_tok.quote)
            return ast.ColumnRef(name=name, quote=quote)

        raise ParseError(f"unexpected {self.describe(tok)} in expression", tok)


def parse(sql):
    """Parse exactly one statement."""
    try:
        tokens = tokenize(sql)
        return Parser(tokens, source=sql).parse_statement()
    except LexError as exc:
        # One error type for callers: lexing failures are parse failures. SQLite's wording for
        # these is already the exception text.
        raise ParseError(exc.message, sqlite_text=str(exc), line=exc.line, col=exc.col) from None
