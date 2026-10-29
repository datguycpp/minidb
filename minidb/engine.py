"""In-memory storage and execution for the MiniDB SQL subset (milestone 2).

Values are Python objects: int, float, str, bytes, None. Type affinity (SQLite's dynamic
typing and column affinity rules) is milestone 17 and deliberately not attempted here — what
milestone 2 promises is that statements execute and that identity errors are reported with
SQLite's own wording.

Error text is part of the contract: SQLite's messages are what the conformance harness
compares, so they are produced here rather than reworded by a caller.
"""

from .sql import ast
from .storage.disk import DiskStorage
from .storage.pager import StorageError

# SQLite's storage-class ordering, used when comparing values of different types.
_RANK = {type(None): 0, int: 1, float: 1, str: 2, bytes: 3}


class ExecError(Exception):
    """Mirrors sqlite3.Error: message text should match SQLite's own where one exists."""


def _rank(value):
    return _RANK[type(value)]


def sql_compare(left, right, op):
    """Three-valued comparison: NULL in either side yields None, as in SQL."""
    if left is None or right is None:
        return None
    lr, rr = _rank(left), _rank(right)
    if lr != rr:
        # Cross-type comparison is defined by storage class, not by trying to coerce.
        if op in ("=", "!="):
            return op == "!="
        return {"<": lr < rr, "<=": lr <= rr, ">": lr > rr, ">=": lr >= rr}[op]
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise ExecError(f"unsupported operator {op}")


def sql_and(a, b):
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def sql_or(a, b):
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def truthy(value):
    """WHERE keeps rows whose predicate is exactly true."""
    return value is True


class Column:
    __slots__ = ("name", "type_name", "not_null", "unique", "primary_key", "default", "check", "check_text")

    def __init__(self, definition: ast.ColumnDef):
        self.name = definition.name
        self.type_name = definition.type_name
        self.not_null = False
        self.unique = False
        self.primary_key = False
        self.default = None
        self.check = None
        self.check_text = None
        for constraint in definition.constraints:
            if constraint.kind == "PRIMARY KEY":
                self.primary_key = True
                self.not_null = True
                self.unique = True
            elif constraint.kind == "NOT NULL":
                self.not_null = True
            elif constraint.kind == "NULL":
                self.not_null = False
            elif constraint.kind == "UNIQUE":
                self.unique = True
            elif constraint.kind == "DEFAULT":
                self.default = constraint.value
            elif constraint.kind == "CHECK":
                self.check = constraint.value
                self.check_text = constraint.text


class Index:
    """Hash index over one or more columns (milestone 4).

    Keys are the column values in index order. NULLs never conflict under SQLite's uniqueness
    rules, so rows containing a NULL key are indexed but exempt from the unique check.
    """

    __slots__ = ("name", "table", "columns", "column_names", "unique", "map")

    def __init__(self, name, table, columns, column_names, unique):
        self.name = name
        self.table = table
        self.columns = columns              # positional indices into the row
        self.column_names = column_names
        self.unique = unique
        self.map = {}                       # key tuple -> set of rowids

    def key_for(self, values):
        return tuple(values[i] for i in self.columns)

    def _conflict_message(self):
        # SQLite names every column of the index: "UNIQUE constraint failed: t.a, t.b"
        return ", ".join(f"{self.table}.{name}" for name in self.column_names)

    def check_unique(self, values):
        """Raise exactly as SQLite does; NULLs never conflict."""
        key = self.key_for(values)
        if self.unique and not any(v is None for v in key) and self.map.get(key):
            raise ExecError(f"UNIQUE constraint failed: {self._conflict_message()}")

    def add(self, rowid, values):
        self.check_unique(values)
        self.map.setdefault(self.key_for(values), set()).add(rowid)

    def lookup(self, key):
        return self.map.get(key, set())


class Table:
    def __init__(self, name, columns, entry=None):
        self.name = name
        self.columns = columns                 # list[Column]
        self.rows = []                          # in-memory rows; unused when `entry` is set
        self.entry = entry                      # catalog entry when backed by disk
        self.index_of = {c.name.lower(): i for i, c in enumerate(columns)}
        self.indexes = {}                       # lower(index name) -> Index
        self.next_rowid = 1                     # in-memory rowids; disk keeps its own counter

    @property
    def on_disk(self):
        return self.entry is not None

    @property
    def column_names(self):
        return [c.name for c in self.columns]


class Database:
    """`Database()` is in-memory; `Database(path)` persists to a page file.

    The path argument is the only API change milestone 3 makes, and callers that never pass it
    behave exactly as before — the conformance runner is untouched and still in-memory.
    """

    def __init__(self, path=None):
        self.path = path
        self.storage = DiskStorage(path) if path else None
        self.tables = {}
        self._sql = None
        self.last_plan = None
        if self.storage is not None:
            self._load_schema()

    def _load_schema(self):
        from .sql.parser import parse
        for entry in self.storage.catalog.values():
            statement = parse(entry["sql"])
            columns = [Column(c) for c in statement.columns]
            table = Table(entry["name"], columns, entry)
            self.tables[entry["name"].lower()] = table
            self._rebuild_indexes(table)

    def close(self):
        if self.storage is not None:
            self.storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _rows(self, table):
        return self.storage.iter_rows(table.entry) if table.on_disk else table.rows

    # -- entry point ---------------------------------------------------------
    def execute(self, statement):
        if isinstance(statement, ast.CreateTable):
            return self._create(statement)
        if isinstance(statement, ast.Insert):
            return self._insert(statement)
        if isinstance(statement, ast.CreateIndex):
            return self._create_index(statement)
        if isinstance(statement, ast.Select):
            return self._select(statement)
        raise ExecError(f"unsupported statement {type(statement).__name__}")

    def run(self, sql):
        from .sql.parser import parse
        self._sql = sql
        return self.execute(parse(sql))

    # -- CREATE --------------------------------------------------------------
    def _create(self, node: ast.CreateTable):
        key = node.name.lower()
        if key in self.tables:
            if node.if_not_exists:
                return None
            raise ExecError(f"table {node.name} already exists")
        seen = set()
        for column in node.columns:
            lname = column.name.lower()
            if lname in seen:
                raise ExecError(f"duplicate column name: {column.name}")
            seen.add(lname)
        columns = [Column(c) for c in node.columns]
        if self.storage is not None:
            entry = self.storage.create_table(node.name, node.sql or self._sql or f"CREATE TABLE {node.name}")
            self.tables[key] = Table(node.name, columns, entry)
        else:
            self.tables[key] = Table(node.name, columns)
        return None

    def table(self, name):
        try:
            return self.tables[name.lower()]
        except KeyError:
            raise ExecError(f"no such table: {name}") from None

    def _create_index(self, node: ast.CreateIndex):
        table = self.table(node.table)
        key = node.name.lower()
        if key in table.indexes:
            if node.if_not_exists:
                return None
            raise ExecError(f"index {node.name} already exists")

        positions = []
        names = []
        for column in node.columns:
            position = table.index_of.get(column.lower())
            if position is None:
                raise ExecError(f"no such column: {column}")
            positions.append(position)
            names.append(column)

        index = Index(node.name, table.name, positions, names, node.unique)
        # Build against existing rows first: a UNIQUE index over duplicate data must fail, and a
        # failed CREATE must leave no half-built index behind.
        for rowid, values in self._rows_with_ids(table):
            index.add(rowid, values)
        table.indexes[key] = index
        if self.storage is not None:
            self._persist_indexes(table)
        return None

    def _persist_indexes(self, table):
        if self.storage is None:
            return
        table.entry["indexes"] = [
            {"name": index.name, "columns": [table.columns[i].name for i in index.columns],
             "unique": index.unique}
            for index in table.indexes.values()
        ]
        self.storage.save_catalog_entry(table.entry)

    def _rebuild_indexes(self, table):
        for definition in table.entry.get("indexes", []):
            names = definition["columns"]
            positions = [table.index_of[c.lower()] for c in names]
            index = Index(definition["name"], table.name, positions, names, definition["unique"])
            for rowid, values in self._rows_with_ids(table):
                index.add(rowid, values)
            table.indexes[definition["name"].lower()] = index

    def _rows_with_ids(self, table):
        if table.on_disk:
            return list(self.storage.iter_rows_with_ids(table.entry))
        return list(enumerate(table.rows, start=1))

    # -- INSERT --------------------------------------------------------------
    def _insert(self, node: ast.Insert):
        table = self.table(node.table)
        if node.columns is None:
            targets = list(range(len(table.columns)))
        else:
            targets = []
            for name in node.columns:
                index = table.index_of.get(name.lower())
                if index is None:
                    raise ExecError(f"table {table.name} has no column named {name}")
                targets.append(index)

        for row in node.rows:
            if len(row) != len(targets):
                raise ExecError(
                    f"table {table.name} has {len(targets)} columns but {len(row)} values were supplied"
                )
            values: list = [None] * len(table.columns)
            supplied = set(targets)
            for index, expr in zip(targets, row):
                values[index] = self._eval(expr, table, None)
            for i, column in enumerate(table.columns):
                if i in supplied:
                    continue
                if column.default is not None:
                    values[i] = self._eval(column.default, table, None)
                elif column.not_null:
                    raise ExecError(f"NOT NULL constraint failed: {table.name}.{column.name}")
            for i, column in enumerate(table.columns):
                if values[i] is None and column.not_null:
                    raise ExecError(f"NOT NULL constraint failed: {table.name}.{column.name}")
                if values[i] is None:
                    continue
                if column.check is not None and not truthy(self._eval(column.check, table, dict(zip(table.column_names, values)))):
                    raise ExecError(f"CHECK constraint failed: {column.check_text}")
                if column.unique:
                    for existing in self._rows(table):
                        if existing[i] == values[i]:
                            raise ExecError(f"UNIQUE constraint failed: {table.name}.{column.name}")
            # Unique indexes are checked before the row lands, so a rejected insert leaves
            # neither a row nor a dangling index entry.
            for index in table.indexes.values():
                index.check_unique(values)
            if table.on_disk:
                try:
                    rowid = self.storage.insert_row(table.entry, values)
                except StorageError as exc:
                    raise ExecError(str(exc)) from None
            else:
                table.rows.append(tuple(values))
                rowid = table.next_rowid
                table.next_rowid += 1
            for index in table.indexes.values():
                index.add(rowid, values)
        return len(node.rows)

    # -- SELECT --------------------------------------------------------------
    def _row_by_id(self, table, rowid):
        if table.on_disk:
            return self.storage.search_row(table.entry, rowid)
        return table.rows[rowid - 1]

    def _pick_index(self, table, where):
        """Only conjunctions of column = literal are index-eligible.

        That is a deliberate floor for M4: OR, ranges and expressions stay full scans until the
        cost-based planner in M21 can reason about them.
        """
        equalities = {}

        def collect(node):
            if isinstance(node, ast.BinaryOp) and node.op == "AND":
                collect(node.left)
                collect(node.right)
            elif isinstance(node, ast.BinaryOp) and node.op == "=":
                left, right = node.left, node.right
                if isinstance(right, ast.ColumnRef) and isinstance(left, ast.Literal):
                    left, right = right, left
                if isinstance(left, ast.ColumnRef) and isinstance(right, ast.Literal):
                    position = table.index_of.get(left.name.lower())
                    if position is not None:
                        equalities[position] = right.value

        collect(where)
        best = None
        for index in table.indexes.values():
            if all(position in equalities for position in index.columns):
                if best is None or len(index.columns) > len(best.columns):
                    best = index
        if best is None:
            return None, None
        return best, tuple(equalities[position] for position in best.columns)

    def _select(self, node: ast.Select):
        table = self.table(node.table)
        # Column headings are a property of the statement, not of the rows: an empty result
        # still has to report the columns it would have returned.
        items = []
        for item in node.items:
            if isinstance(item, ast.Star):
                items.extend(table.column_names)
            else:
                items.append(item.alias or _describe(item.expr))

        index, key = (None, None)
        if node.where is not None:
            index, key = self._pick_index(table, node.where)

        if index is not None:
            candidates = [(rowid, self._row_by_id(table, rowid)) for rowid in sorted(index.lookup(key))]
            access, examined, index_name = "index", len(candidates), index.name
        else:
            candidates = list(self._rows_with_ids(table))
            access, examined, index_name = "full", len(candidates), None

        rows = []
        for _, row in candidates:
            if row is None:
                continue
            env = dict(zip((c.name.lower() for c in table.columns), row))
            if node.where is not None and not truthy(self._eval(node.where, table, env)):
                continue
            projected = []
            for item in node.items:
                if isinstance(item, ast.Star):
                    projected.extend(row)
                else:
                    projected.append(self._eval(item.expr, table, env))
            rows.append(tuple(projected))

        self.last_plan = {"table": table.name, "access": access, "index": index_name,
                          "rows_examined": examined, "rows_matched": len(rows)}

        if node.distinct:
            seen, unique = set(), []
            for row in rows:
                if row not in seen:
                    seen.add(row)
                    unique.append(row)
            rows = unique
        return items, rows

    # -- expressions ---------------------------------------------------------
    def _eval(self, node, table, env):
        if isinstance(node, ast.Literal):
            return node.value
        if isinstance(node, ast.ColumnRef):
            key = node.name.lower()
            if env is None or key not in env:
                # SQLite adds the hint only for double-quoted identifiers, and prints the
                # quotes back; bracket/backtick forms lose their quotes in the message.
                if node.quote == '"':
                    raise ExecError(
                        f'no such column: "{node.name}" - should this be a string literal in single-quotes?'
                    )
                raise ExecError(f"no such column: {node.name}")
            return env[key]
        if isinstance(node, ast.BinaryOp):
            if node.op == "AND":
                return sql_and(self._eval(node.left, table, env), self._eval(node.right, table, env))
            if node.op == "OR":
                return sql_or(self._eval(node.left, table, env), self._eval(node.right, table, env))
            return sql_compare(self._eval(node.left, table, env), self._eval(node.right, table, env), node.op)
        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand, table, env)
            if node.op == "NOT":
                return None if value is None else not truthy(value)
            if node.op == "-":
                return None if value is None else -value
            if node.op == "+":
                return value
        if isinstance(node, ast.IsNull):
            value = self._eval(node.expr, table, env)
            result = value is None
            return (not result) if node.negated else result
        raise ExecError(f"cannot evaluate {type(node).__name__}")


def _describe(expr):
    """Column heading SQLite would use for an unaliased expression."""
    if isinstance(expr, ast.ColumnRef):
        return expr.name
    if isinstance(expr, ast.Literal):
        return str(expr.value)
    return "?"
