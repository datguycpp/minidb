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


CONSTRAINT_STARTERS = {"PRIMARY", "NOT", "NULL", "UNIQUE", "DEFAULT", "CHECK", "REFERENCES",
                       "COLLATE"}

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
        elif self.at_keyword("WITH"):
            stmt = self.parse_with()
        elif self.at_keyword("DROP"):
            stmt = self.parse_drop()
        elif self.at_keyword("ALTER"):
            stmt = self.parse_alter()
        elif self.at_keyword("UPDATE"):
            stmt = self.parse_update()
        elif self.at_keyword("DELETE"):
            stmt = self.parse_delete()
        elif self.at_keyword("PRAGMA"):
            stmt = self.parse_pragma()
        elif self.at_keyword("VACUUM"):
            self.advance()
            stmt = ast.Vacuum()
        elif self.at_keyword("ANALYZE"):
            self.advance()
            target = None
            if self.tok.kind in ("IDENT", "KEYWORD"):
                target = self.advance().value
            stmt = ast.Analyze(target=target)
        elif self.at_keyword("SELECT"):
            stmt = self.parse_compound()
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
        if self.at_keyword("TRIGGER"):
            return self.parse_create_trigger(False)
        if self.at_keyword("VIEW"):
            return self.parse_create_view(False)
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
        table_constraints = []
        if self.at_punct(")"):
            raise ParseError("CREATE TABLE needs at least one column", self.tok)
        while True:
            if self.at_keyword("PRIMARY") or self.at_keyword("FOREIGN") \
                    or self.at_keyword("CHECK") or self.at_keyword("UNIQUE"):
                table_constraints.append(self.parse_table_constraint())
            else:
                columns.append(self.parse_column_def())
            if self.accept_punct(","):
                continue
            break
        self.expect_punct(")")
        statement = ast.CreateTable(name=name, columns=columns,
                                    table_constraints=table_constraints,
                                    if_not_exists=if_not_exists)
        # The catalog keeps the CREATE statement verbatim so reopening needs no AST serialiser.
        statement.sql = self.source.strip() if self.source else None
        return statement

    def parse_create_trigger(self, if_not_exists):
        self.expect_keyword("TRIGGER")
        if self.accept_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            if_not_exists = True
        name = self.parse_identifier("trigger name")
        timing = "BEFORE"
        if self.accept_keyword("BEFORE"):
            timing = "BEFORE"
        elif self.accept_keyword("AFTER"):
            timing = "AFTER"
        if self.at_keyword("INSERT"):
            event = "INSERT"
            self.advance()
        elif self.at_keyword("UPDATE"):
            event = "UPDATE"
            self.advance()
        elif self.at_keyword("DELETE"):
            event = "DELETE"
            self.advance()
        else:
            raise ParseError("expected INSERT, UPDATE or DELETE in trigger", self.tok)
        self.expect_keyword("ON")
        table = self.parse_identifier("table name")
        if self.accept_keyword("FOR"):
            self.expect_keyword("EACH")
            self.expect_keyword("ROW")
        when = self.parse_expression() if self.accept_keyword("WHEN") else None
        self.expect_keyword("BEGIN")
        statements = []
        while not self.at_keyword("END"):
            statements.append(self.parse_trigger_statement())
            self.accept_punct(";")
        self.expect_keyword("END")
        return ast.CreateTrigger(name=name, timing=timing, event=event, table=table, when=when,
                                 statements=statements, if_not_exists=if_not_exists,
                                 sql=self.source.strip() if self.source else None)

    def parse_trigger_statement(self):
        if self.at_keyword("INSERT"):
            return self.parse_insert()
        if self.at_keyword("UPDATE"):
            return self.parse_update()
        if self.at_keyword("DELETE"):
            return self.parse_delete()
        if self.at_keyword("SELECT"):
            return self.parse_compound()
        raise ParseError("unsupported statement inside a trigger body", self.tok)

    def parse_create_view(self, if_not_exists):
        self.expect_keyword("VIEW")
        if self.accept_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            if_not_exists = True
        name = self.parse_identifier("view name")
        columns = []
        if self.accept_punct("("):
            columns.append(self.parse_identifier("column name"))
            while self.accept_punct(","):
                columns.append(self.parse_identifier("column name"))
            self.expect_punct(")")
        self.expect_keyword("AS")
        select = self.parse_compound()
        return ast.CreateView(name=name, columns=columns, select=select,
                              if_not_exists=if_not_exists, sql=getattr(self, 'source', None))

    def parse_pragma(self):
        self.expect_keyword("PRAGMA")
        name = self.parse_identifier("pragma name")
        value = None
        if self.accept_punct("="):
            if self.at_keyword("ON"):
                self.advance()
                value = ast.Literal(1, "number")
            elif self.at_keyword("OFF"):
                self.advance()
                value = ast.Literal(0, "number")
            else:
                value = self.parse_expression()
        elif self.at_punct("("):
            self.advance()
            value = self.parse_expression()
            self.expect_punct(")")
        return ast.Pragma(name=name, value=value)

    def parse_alter(self):
        self.expect_keyword("ALTER")
        self.expect_keyword("TABLE")
        table = self.parse_identifier("table name")
        if self.accept_keyword("ADD"):
            self.accept_keyword("COLUMN")
            return ast.AlterTable(table=table, action="add", column=self.parse_column_def())
        if self.accept_keyword("RENAME"):
            if self.accept_keyword("TO"):
                return ast.AlterTable(table=table, action="rename_table",
                                      new_name=self.parse_identifier("table name"))
            self.accept_keyword("COLUMN")
            old = self.parse_identifier("column name")
            self.expect_keyword("TO")
            return ast.AlterTable(table=table, action="rename_column", old_name=old,
                                  new_name=self.parse_identifier("column name"))
        if self.accept_keyword("DROP"):
            self.accept_keyword("COLUMN")
            return ast.AlterTable(table=table, action="drop_column",
                                  old_name=self.parse_identifier("column name"))
        raise ParseError("expected ADD, RENAME or DROP after ALTER TABLE", self.tok)

    def parse_update(self):
        self.expect_keyword("UPDATE")
        table = self.parse_identifier("table name")
        self.expect_keyword("SET")
        assignments = []
        while True:
            column = self.parse_identifier("column name")
            self.expect_punct("=")
            assignments.append((column, self.parse_expression()))
            if not self.accept_punct(","):
                break
        where = self.parse_expression() if self.accept_keyword("WHERE") else None
        return ast.Update(table=table, assignments=assignments, where=where)

    def parse_delete(self):
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.parse_identifier("table name")
        where = self.parse_expression() if self.accept_keyword("WHERE") else None
        return ast.Delete(table=table, where=where)

    def parse_drop(self):
        self.expect_keyword("DROP")
        if self.at_keyword("TABLE"):
            self.advance()
            if_exists = False
            if self.accept_keyword("IF"):
                self.expect_keyword("EXISTS")
                if_exists = True
            return ast.DropTable(name=self.parse_identifier("table name"), if_exists=if_exists)
        if self.at_keyword("TRIGGER"):
            self.advance()
            if_exists = False
            if self.accept_keyword("IF"):
                self.expect_keyword("EXISTS")
                if_exists = True
            return ast.DropTrigger(name=self.parse_identifier("trigger name"), if_exists=if_exists)
        if not self.at_keyword("VIEW"):
            raise ParseError("only DROP VIEW is supported", self.tok)
        self.advance()
        if_exists = False
        if self.accept_keyword("IF"):
            self.expect_keyword("EXISTS")
            if_exists = True
        name = self.parse_identifier("view name")
        return ast.DropView(name=name, if_exists=if_exists)

    def parse_with(self):
        self.expect_keyword("WITH")
        recursive = bool(self.accept_keyword("RECURSIVE"))
        ctes = []
        while True:
            name = self.parse_identifier("CTE name")
            columns = []
            if self.accept_punct("("):
                columns.append(self.parse_identifier("column name"))
                while self.accept_punct(","):
                    columns.append(self.parse_identifier("column name"))
                self.expect_punct(")")
            self.expect_keyword("AS")
            self.expect_punct("(")
            select = self.parse_compound()
            self.expect_punct(")")
            ctes.append(ast.CTE(name=name, columns=columns, select=select, recursive=recursive))
            if not self.accept_punct(","):
                break
        body = self.parse_compound()
        return ast.With(ctes=ctes, body=body)

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
        where = self.parse_expression() if self.accept_keyword("WHERE") else None
        statement = ast.CreateIndex(name=name, table=table, columns=columns,
                                    unique=unique, if_not_exists=if_not_exists, where=where)
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

    def parse_table_constraint(self):
        if self.accept_keyword("PRIMARY"):
            self.expect_keyword("KEY")
            self.expect_punct("(")
            columns = [self.parse_identifier("column name")]
            while self.accept_punct(","):
                columns.append(self.parse_identifier("column name"))
            self.expect_punct(")")
            return ast.TableConstraint("PRIMARY KEY", columns=columns)
        if self.accept_keyword("UNIQUE"):
            self.expect_punct("(")
            columns = [self.parse_identifier("column name")]
            while self.accept_punct(","):
                columns.append(self.parse_identifier("column name"))
            self.expect_punct(")")
            return ast.TableConstraint("UNIQUE", columns=columns)
        if self.accept_keyword("FOREIGN"):
            self.expect_keyword("KEY")
            self.expect_punct("(")
            columns = [self.parse_identifier("column name")]
            while self.accept_punct(","):
                columns.append(self.parse_identifier("column name"))
            self.expect_punct(")")
            self.expect_keyword("REFERENCES")
            ref_table = self.parse_identifier("table name")
            ref_columns = []
            if self.accept_punct("("):
                ref_columns.append(self.parse_identifier("column name"))
                while self.accept_punct(","):
                    ref_columns.append(self.parse_identifier("column name"))
                self.expect_punct(")")
            return ast.TableConstraint("FOREIGN KEY", columns=columns, ref_table=ref_table,
                                       ref_columns=ref_columns)
        self.expect_keyword("CHECK")
        open_paren = self.expect_punct("(")
        expr = self.parse_expression()
        close_paren = self.tok
        self.expect_punct(")")
        text = self.source[open_paren.pos + 1:close_paren.pos].strip()
        return ast.TableConstraint("CHECK", expr=expr, text=text)

    def parse_column_constraint(self):
        if self.accept_keyword("PRIMARY"):
            self.expect_keyword("KEY")
            constraint = ast.Constraint("PRIMARY KEY")
            if self.accept_keyword("AUTOINCREMENT"):
                constraint.text = "AUTOINCREMENT"
            return constraint
        if self.accept_keyword("NOT"):
            self.expect_keyword("NULL")
            return ast.Constraint("NOT NULL")
        if self.accept_keyword("NULL"):
            return ast.Constraint("NULL")
        if self.accept_keyword("UNIQUE"):
            return ast.Constraint("UNIQUE")
        if self.accept_keyword("DEFAULT"):
            return ast.Constraint("DEFAULT", self.parse_expression())
        if self.accept_keyword("COLLATE"):
            return ast.Constraint("COLLATE", text=self.parse_identifier("collation name"))
        if self.accept_keyword("REFERENCES"):
            ref_table = self.parse_identifier("table name")
            ref_column = None
            if self.accept_punct("("):
                ref_column = self.parse_identifier("column name")
                self.expect_punct(")")
            return ast.Constraint("REFERENCES", ref_table=ref_table, ref_column=ref_column)
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

        table = None
        alias = None
        joins = []
        base_select = None
        if self.accept_keyword("FROM"):
            table, alias, base_select = self.parse_source()
            while True:
                kind = self.parse_join_kind()
                if kind:
                    joins.append(self.parse_join_body(kind))
                elif self.accept_punct(","):
                    joins.append(self.parse_join_body("cross"))
                else:
                    break

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
                          alias=alias, base_select=base_select, joins=joins,
                          group_by=group_by, having=having)

    def parse_select_tail(self):
        """ORDER BY / LIMIT. Parsed once, at the end of a whole compound expression."""
        order_by = []
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            while True:
                expression = self.parse_expression()
                descending = False
                if self.accept_keyword("DESC"):
                    descending = True
                else:
                    self.accept_keyword("ASC")
                order_by.append((expression, descending))
                if not self.accept_punct(","):
                    break

        limit = None
        offset = None
        if self.accept_keyword("LIMIT"):
            first = self.parse_additive()
            if self.accept_punct(","):
                offset = first
                limit = self.parse_additive()
            else:
                limit = first
                if self.accept_keyword("OFFSET"):
                    offset = self.parse_additive()
        return order_by, limit, offset

    def parse_compound(self):
        """Set operations, all at equal precedence and left-associative.

        SQLite is not the SQL standard here: `a UNION b INTERSECT c` means `(a UNION b) INTERSECT c`,
        not `a UNION (b INTERSECT c)`. The reference decides, so this follows the reference.
        """
        left = self.parse_select()
        while True:
            if self.at_keyword("UNION") or self.at_keyword("EXCEPT"):
                op = self.advance().upper
                if op == "UNION" and self.accept_keyword("ALL"):
                    op = "UNION ALL"
            elif self.at_keyword("INTERSECT"):
                self.advance()
                op = "INTERSECT"
            else:
                break
            right = self.parse_select()
            left = ast.Compound(left=left, op=op, right=right)

        order_by, limit, offset = self.parse_select_tail()
        left.order_by = order_by
        left.limit = limit
        left.offset = offset
        return left

    def parse_source(self):
        """A table name or a derived table: `FROM t`, `FROM t AS x`, `FROM (SELECT ...) AS x`."""
        if self.at_punct("("):
            self.advance()
            if not self.at_keyword("SELECT"):
                raise ParseError("expected a subquery after ( in FROM", self.tok)
            select = self.parse_compound()
            self.expect_punct(")")
            alias = self.parse_optional_alias()
            return None, alias, select
        name = self.parse_identifier("table name")
        alias = self.parse_optional_alias()
        return name, alias, None

    def parse_join_kind(self):
        if self.at_keyword("JOIN"):
            self.advance()
            return "inner"
        if self.at_keyword("INNER"):
            self.advance()
            self.expect_keyword("JOIN")
            return "inner"
        for word, kind in (("LEFT", "left"), ("RIGHT", "right"), ("FULL", "full")):
            if self.at_keyword(word):
                self.advance()
                self.accept_keyword("OUTER")
                self.expect_keyword("JOIN")
                return kind
        return None

    def parse_join_body(self, kind):
        if self.at_punct("("):
            self.advance()
            select = self.parse_compound()
            self.expect_punct(")")
            alias = self.parse_optional_alias()
            join = ast.Join(select=select, alias=alias, kind=kind)
        else:
            join_table = self.parse_identifier("table name")
            join_alias = self.parse_optional_alias()
            join = ast.Join(table=join_table, alias=join_alias, kind=kind)
        if self.accept_keyword("ON"):
            join.on = self.parse_expression()
        return join

    def parse_optional_alias(self):
        if self.accept_keyword("AS"):
            return self.parse_identifier("alias")
        if self.tok.kind == "IDENT":
            return self.advance().value
        return None

    def parse_window(self, name, arguments):
        self.expect_punct("(")
        partition_by = []
        order_by = []
        frame = None
        if self.accept_keyword("PARTITION"):
            self.expect_keyword("BY")
            partition_by.append(self.parse_expression())
            while self.accept_punct(","):
                partition_by.append(self.parse_expression())
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            while True:
                expression = self.parse_expression()
                descending = False
                if self.accept_keyword("DESC"):
                    descending = True
                else:
                    self.accept_keyword("ASC")
                order_by.append((expression, descending))
                if not self.accept_punct(","):
                    break
        if self.at_keyword("ROWS") or self.at_keyword("RANGE"):
            mode = self.advance().upper.lower()
            self.expect_keyword("BETWEEN")
            start = self.parse_frame_bound()
            self.expect_keyword("AND")
            end = self.parse_frame_bound()
            frame = (mode, start, end)
        self.expect_punct(")")
        return ast.WindowFunc(name=name, args=arguments, partition_by=partition_by,
                              order_by=order_by, frame=frame)

    def parse_frame_bound(self):
        if self.accept_keyword("UNBOUNDED"):
            if self.accept_keyword("PRECEDING"):
                return ("unbounded", "preceding")
            self.expect_keyword("FOLLOWING")
            return ("unbounded", "following")
        if self.accept_keyword("CURRENT"):
            self.expect_keyword("ROW")
            return ("current", None)
        value = self.parse_additive()
        if self.accept_keyword("PRECEDING"):
            return ("preceding", value)
        self.expect_keyword("FOLLOWING")
        return ("following", value)

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

    def parse_additive(self):
        node = self.parse_multiplicative()
        while self.tok.kind == "PUNCT" and self.tok.value in ("+", "-", "||"):
            op = self.advance().value
            node = ast.BinaryOp(op, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self):
        node = self.parse_unary()
        while self.tok.kind == "PUNCT" and self.tok.value in ("*", "/", "%"):
            op = self.advance().value
            node = ast.BinaryOp(op, node, self.parse_unary())
        return node

    def parse_collate(self, node):
        while self.accept_keyword("COLLATE"):
            node = ast.Collate(expr=node, name=self.parse_identifier("collation name"))
        return node

    def parse_unary(self):
        if self.tok.kind == "PUNCT" and self.tok.value in ("-", "+"):
            op = self.advance().value
            return ast.UnaryOp(op, self.parse_unary())
        return self.parse_collate(self.parse_primary())

    def parse_comparison(self):
        node = self.parse_additive()

        if self.accept_keyword("IS"):
            negated = bool(self.accept_keyword("NOT"))
            if self.accept_keyword("NULL"):
                return ast.IsNull(node, negated=negated)
            return ast.BinaryOp("IS NOT" if negated else "IS", node, self.parse_additive())

        negated = False
        if self.at_keyword("NOT") and self.peek().kind == "KEYWORD" and self.peek().upper in ("BETWEEN", "IN"):
            self.advance()
            negated = True

        if self.accept_keyword("BETWEEN"):
            low = self.parse_additive()
            self.expect_keyword("AND")
            high = self.parse_additive()
            return ast.Between(expr=node, low=low, high=high, negated=negated)

        if self.accept_keyword("IN"):
            self.expect_punct("(")
            if not self.at_keyword("SELECT"):
                raise ParseError("only IN (SELECT ...) is supported", self.tok)
            inner = self.parse_compound()
            self.expect_punct(")")
            return ast.InSubquery(expr=node, select=inner, negated=negated)

        if negated:
            raise ParseError("expected BETWEEN or IN after NOT", self.tok)

        if self.tok.kind == "PUNCT" and self.tok.value in COMPARISON_OPS:
            op = self.advance().value
            if op == "==":
                op = "="
            elif op == "<>":
                op = "!="
            return ast.BinaryOp(op, node, self.parse_additive())
        return node

    def parse_case(self):
        self.expect_keyword("CASE")
        operand = None
        if not self.at_keyword("WHEN"):
            operand = self.parse_expression()
        whens = []
        while self.accept_keyword("WHEN"):
            condition = self.parse_expression()
            self.expect_keyword("THEN")
            whens.append((condition, self.parse_expression()))
        if not whens:
            raise ParseError("CASE needs at least one WHEN", self.tok)
        else_expr = None
        if self.accept_keyword("ELSE"):
            else_expr = self.parse_expression()
        self.expect_keyword("END")
        return ast.Case(operand=operand, whens=whens, else_expr=else_expr)

    def parse_primary(self):
        tok = self.tok

        if tok.kind == "KEYWORD" and tok.upper == "CASE":
            return self.parse_case()

        if tok.kind == "KEYWORD" and tok.upper == "CAST" and self.peek().kind == "PUNCT" and self.peek().value == "(":
            self.advance()
            self.expect_punct("(")
            inner = self.parse_expression()
            self.expect_keyword("AS")
            type_name = self.parse_identifier("type name").upper()
            self.expect_punct(")")
            return ast.Cast(expr=inner, type_name=type_name)

        if tok.kind == "PUNCT" and tok.value == "(":
            self.advance()
            if self.at_keyword("SELECT"):
                inner = self.parse_compound()
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

        if tok.kind == "BLOB":
            self.advance()
            return ast.Literal(tok.value, "blob")
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

        if tok.kind in ("IDENT", "KEYWORD") and self.peek().kind == "PUNCT" and self.peek().value == "(" \
                and tok.upper not in RESERVED_IDENTIFIERS:
            name = self.advance().upper
            if name == "RAISE":
                self.expect_punct("(")
                action = self.parse_identifier("RAISE action").upper()
                message = None
                if self.accept_punct(","):
                    token = self.advance()
                    message = token.value
                self.expect_punct(")")
                return ast.Raise(action=action, message=message)
            self.expect_punct("(")
            if self.at_punct("*"):
                self.advance()
                self.expect_punct(")")
                return ast.FuncCall(name=name, arg=None, star=True, args=[])
            arguments = []
            distinct = False
            if not self.at_punct(")"):
                distinct = bool(self.accept_keyword("DISTINCT"))
                arguments.append(self.parse_expression())
                while self.accept_punct(","):
                    arguments.append(self.parse_expression())
            self.expect_punct(")")
            if self.at_keyword("OVER"):
                self.advance()
                return self.parse_window(name, arguments)
            return ast.FuncCall(name=name, arg=arguments[0] if arguments else None,
                                distinct=distinct, args=arguments)

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
