"""In-memory storage and execution for the MiniDB SQL subset (milestone 2).

Values are Python objects: int, float, str, bytes, None. Type affinity (SQLite's dynamic
typing and column affinity rules) is milestone 17 and deliberately not attempted here — what
milestone 2 promises is that statements execute and that identity errors are reported with
SQLite's own wording.

Error text is part of the contract: SQLite's messages are what the conformance harness
compares, so they are produced here rather than reworded by a caller.
"""

import copy
import re

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


_NUMERIC_PREFIX = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def to_number(value):
    """SQLite's numeric coercion: text takes its leading numeric prefix, everything else 0."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        match = _NUMERIC_PREFIX.match(value.strip())
        if not match:
            return 0
        text = match.group(0)
        if "." in text or "e" in text.lower():
            return float(text)
        return int(text)
    return 0


def to_text(value):
    """Text conversion matching SQLite: integral floats keep a .0, exponents keep .0e."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e16:
            return f"{int(value)}.0"
        text = f"{value:.15g}"
        if "e" in text and "." not in text.split("e")[0]:
            mantissa, exponent = text.split("e")
            text = f"{mantissa}.0e{exponent}"
        return text
    return str(value)


def truncating_divide(left, right):
    """SQLite integer division truncates toward zero; Python's // floors."""
    if right == 0:
        return None
    quotient = abs(left) // abs(right)
    return -quotient if (left < 0) != (right < 0) else quotient


def arithmetic(op, left, right):
    if left is None or right is None:
        return None
    if op == "||":
        return to_text(left) + to_text(right)
    a, b = to_number(left), to_number(right)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if isinstance(a, int) and isinstance(b, int):
            return truncating_divide(a, b)
        return None if b == 0 else a / b
    if op == "%":
        if isinstance(a, int) and isinstance(b, int):
            quotient = truncating_divide(a, b)
            return None if quotient is None else a - b * quotient
        return None if b == 0 else a - b * int(a / b)
    raise ExecError(f"unsupported operator {op}")


def sql_is(left, right):
    """IS is NULL-safe equality and never returns NULL."""
    if left is None or right is None:
        return left is None and right is None
    return left == right


def cast_value(value, type_name):
    if value is None:
        return None
    if type_name in ("INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT"):
        number = to_number(value)
        return 0 if number is None else int(number)
    if type_name in ("REAL", "DOUBLE", "FLOAT"):
        number = to_number(value)
        return 0.0 if number is None else float(number)
    if type_name in ("TEXT", "CLOB", "VARCHAR", "CHAR"):
        return to_text(value)
    if type_name in ("NUMERIC", "DECIMAL", "BOOLEAN"):
        number = to_number(value)
        return number if number is not None else 0
    return to_text(value)


def sql_and(a, b):
    """Three-valued AND: a false operand decides even when the other is unknown."""
    if a is not None and not truthy(a):
        return False
    if b is not None and not truthy(b):
        return False
    if a is None or b is None:
        return None
    return True


def sql_or(a, b):
    if a is not None and truthy(a):
        return True
    if b is not None and truthy(b):
        return True
    if a is None or b is None:
        return None
    return False


def truthy(value):
    """SQLite truthiness: numbers are true when non-zero, text by its numeric prefix."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return to_number(value) != 0
    return bool(value)


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
        self.tx_snapshot = None
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

    # -- transactions --------------------------------------------------------
    def _begin(self):
        if self.tx_snapshot is not None:
            raise ExecError("cannot start a transaction within a transaction")
        # On disk, uncommitted pages simply stay in the pager's dirty buffer, so rollback needs
        # no snapshot. In memory there is no log, so the copy *is* the rollback mechanism.
        self.tx_snapshot = True if self.storage is not None else copy.deepcopy(self.tables)
        return None

    def _commit(self):
        if self.tx_snapshot is None:
            raise ExecError("cannot commit - no transaction is active")
        if self.storage is not None:
            self.storage.commit()
        self.tx_snapshot = None
        return None

    def _rollback(self):
        if self.tx_snapshot is None:
            raise ExecError("cannot rollback - no transaction is active")
        if self.storage is not None:
            self.storage.rollback()
            self.tables = {}
            self._load_schema()
        else:
            self.tables = self.tx_snapshot
        self.tx_snapshot = None
        return None

    def close(self):
        # SQLite rolls back an open transaction when the connection closes; match it.
        if self.tx_snapshot is not None:
            self._rollback()
        if self.storage is not None:
            self.storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _rows(self, table):
        return self.storage.iter_rows(table.entry) if table.on_disk else table.rows

    # -- entry point ---------------------------------------------------------
    def execute(self, statement, autocommit=False):
        """Run one statement. `autocommit` is explicit because run() and execute() differ:

        run() mirrors the sqlite3 CLI — every statement outside a transaction is durable when it
        returns. execute() is the statement primitive and leaves that choice to the caller.
        """
        result = self._execute(statement)
        if (autocommit and self.storage is not None and self.tx_snapshot is None
                and not isinstance(statement, ast.Begin)):
            self.storage.commit()
        return result

    def _execute(self, statement):
        if isinstance(statement, ast.CreateTable):
            return self._create(statement)
        if isinstance(statement, ast.Insert):
            return self._insert(statement)
        if isinstance(statement, ast.Begin):
            return self._begin()
        if isinstance(statement, ast.Commit):
            return self._commit()
        if isinstance(statement, ast.Rollback):
            return self._rollback()
        if isinstance(statement, ast.CreateIndex):
            return self._create_index(statement)
        if isinstance(statement, ast.Select):
            return self._select(statement)
        raise ExecError(f"unsupported statement {type(statement).__name__}")

    def run(self, sql):
        from .sql.parser import parse
        self._sql = sql
        return self.execute(parse(sql), autocommit=True)

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

    def _equalities(self, table, where):
        """Equality constraints usable by an index: only conjunctions of column = literal.

        OR, ranges and expressions stay full scans until the cost-based planner (M21) can price
        them; half-supporting them now would mean choosing an index that returns wrong answers.
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
        return equalities

    def _row_count(self, table):
        # No DELETE exists yet, so the next rowid is exactly the number of rows.
        return table.entry["next_rowid"] - 1 if table.on_disk else len(table.rows)

    def _choose_access(self, table, where):
        """M5's choice layer. The rule, in order:

        1. No WHERE, or no index whose every column is constrained -> full scan.
        2. Estimate each eligible index as `len(bucket)` for the equality key: the number of rows
           a lookup would hand to the filter.
        3. Prefer the smallest estimate; break ties by more constrained columns, then unique,
           then name. Deterministic, so the same query always takes the same path.
        4. If the best estimate is not smaller than the full table, scan instead — an index that
           touches everything only adds a lookup.
        """
        total = self._row_count(table)
        if where is None:
            return {"access": "full", "reason": "no-predicate", "candidates": [], "estimated_rows": total}

        equalities = self._equalities(table, where)
        candidates = []
        for index in table.indexes.values():
            if all(position in equalities for position in index.columns):
                key = tuple(equalities[position] for position in index.columns)
                candidates.append({
                    "index": index,
                    "key": key,
                    "estimated_rows": len(index.lookup(key)),
                    "columns": [table.columns[i].name for i in index.columns],
                })

        summary = [{"name": c["index"].name, "columns": c["columns"],
                    "estimated_rows": c["estimated_rows"]} for c in candidates]
        if not candidates:
            return {"access": "full", "reason": "no-eligible-index", "candidates": summary,
                    "estimated_rows": total}

        best = min(candidates, key=lambda c: (c["estimated_rows"], -len(c["columns"]),
                                              not c["index"].unique, c["index"].name))
        if best["estimated_rows"] >= total:
            return {"access": "full", "reason": "index-not-selective", "candidates": summary,
                    "estimated_rows": total}

        return {"access": "index", "reason": "smallest-estimate", "candidate": best,
                "candidates": summary, "estimated_rows": best["estimated_rows"]}

    # -- query execution -----------------------------------------------------
    AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})

    def _contains_aggregate(self, node):
        if isinstance(node, ast.FuncCall):
            return node.name in self.AGGREGATES
        if isinstance(node, ast.BinaryOp):
            return self._contains_aggregate(node.left) or self._contains_aggregate(node.right)
        if isinstance(node, ast.UnaryOp):
            return self._contains_aggregate(node.operand)
        if isinstance(node, ast.IsNull):
            return self._contains_aggregate(node.expr)
        return False

    def _is_complex(self, node):
        return bool(node.table is None or node.base_select is not None
                    or node.joins or node.group_by or node.having
                    or any(self._contains_aggregate(item.expr)
                           for item in node.items if not isinstance(item, ast.Star)))

    def _sources(self, node):
        """Source list: base table or derived table, then one entry per JOIN."""
        out = []
        if node.base_select is not None:
            columns, rows = self._select(node.base_select)
            out.append({"alias": (node.alias or "subquery").lower(),
                        "columns": columns, "rows": rows, "kind": "base", "on": None})
        elif node.table is not None:
            table = self.table(node.table)
            out.append({"alias": (node.alias or node.table).lower(),
                        "columns": table.column_names,
                        "rows": [row for _, row in self._rows_with_ids(table)],
                        "kind": "base", "on": None})
        for join in node.joins:
            alias = (join.alias or join.table or "subquery").lower()
            if join.select is not None:
                columns, rows = self._select(join.select)
            else:
                table = self.table(join.table)
                columns = table.column_names
                rows = [row for _, row in self._rows_with_ids(table)]
            out.append({"alias": alias, "columns": columns, "rows": rows,
                        "kind": join.kind, "on": join.on})
        return out

    def _scan_sources(self, node):
        """Nested loop over the sources, honouring INNER/LEFT/RIGHT/FULL join semantics."""
        sources = self._sources(node)
        examined = 0
        if not sources:
            return [{}], 1                     # FROM-less SELECT: one empty row

        counts = {}
        for source in sources:
            for column in source["columns"]:
                counts[column.lower()] = counts.get(column.lower(), 0) + 1

        def build_env(parts):
            env = {}
            for (alias, values), source in zip(parts, sources):
                for position, column in enumerate(source["columns"]):
                    value = None if values is None else values[position]
                    env[f"{alias}.{column.lower()}"] = value
                    if counts[column.lower()] == 1:
                        env[column.lower()] = value
            return env

        states = [[(sources[0]["alias"], row)] for row in sources[0]["rows"]]
        for index in range(1, len(sources)):
            source = sources[index]
            kind = source["kind"]
            join = source["on"]
            new_states = []
            matched_right = set()
            for state in states:
                matched = False
                for right_index, row in enumerate(source["rows"]):
                    examined += 1
                    candidate = state + [(source["alias"], row)]
                    if join is None or truthy(self._eval(join, None, build_env(candidate))):
                        matched = True
                        matched_right.add(right_index)
                        new_states.append(candidate)
                if not matched and kind in ("left", "full"):
                    new_states.append(state + [(source["alias"], None)])
            if kind in ("right", "full"):
                for right_index, row in enumerate(source["rows"]):
                    if right_index not in matched_right:
                        new_states.append([(s["alias"], None) for s in sources[:index]]
                                          + [(source["alias"], row)])
            states = new_states

        return [build_env(state) for state in states], examined

    def _select(self, node: ast.Select):
        if self._is_complex(node):
            return self._select_complex(node)
        return self._select_simple(node)

    def _select_simple(self, node: ast.Select):
        table = self.table(node.table)
        # Column headings are a property of the statement, not of the rows: an empty result
        # still has to report the columns it would have returned.
        items = []
        for item in node.items:
            if isinstance(item, ast.Star):
                items.extend(table.column_names)
            else:
                items.append(item.alias or _heading(item))

        decision = self._choose_access(table, node.where)
        if decision["access"] == "index":
            chosen = decision["candidate"]
            candidates = [(rowid, self._row_by_id(table, rowid))
                          for rowid in sorted(chosen["index"].lookup(chosen["key"]))]
        else:
            candidates = list(self._rows_with_ids(table))
        access = decision["access"]
        examined = len(candidates)
        index_name = decision["candidate"]["index"].name if access == "index" else None

        rows = []
        for _, row in candidates:
            if row is None:
                continue
            # Both bare and qualified names: `SELECT t.a FROM t` is valid SQLite on a single
            # table, and multi-table work made the qualified form obvious to write.
            env = {}
            for column, value in zip(table.columns, row):
                env[column.name.lower()] = value
                env[f"{table.name.lower()}.{column.name.lower()}"] = value
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
                          "rows_examined": examined, "rows_matched": len(rows),
                          "reason": decision["reason"],
                          "estimated_rows": decision["estimated_rows"],
                          "candidates": decision["candidates"]}

        if node.distinct:
            rows = _distinct(rows)
        return items, rows

    def _select_complex(self, node: ast.Select):
        """Joins, aggregates, GROUP BY/HAVING. Nested loop, planned and reported as such."""
        envs, examined = self._scan_sources(node)
        if node.where is not None:
            envs = [env for env in envs if truthy(self._eval(node.where, None, env))]

        source_list = self._sources(node)
        if any(isinstance(item, ast.Star) for item in node.items) and not source_list:
            raise ExecError("no tables specified")

        projection = []
        items = []
        for item in node.items:
            if isinstance(item, ast.Star):
                for source in source_list:
                    for column in source["columns"]:
                        ref = ast.ColumnRef(name=column, table=source["alias"])
                        projection.append(ast.SelectItem(expr=ref))
                        items.append(column)
            else:
                projection.append(item)
                items.append(item.alias or _heading(item))

        rows = []
        if node.group_by or any(self._contains_aggregate(i.expr) for i in node.items
                                if not isinstance(i, ast.Star)):
            groups = {}
            order = []
            for env in envs:
                key = tuple(self._eval(expression, None, env) for expression in node.group_by)
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(env)
            if not node.group_by:
                order = [()]
                groups = {(): envs}
            else:
                # SQLite groups through a sorted structure, so an unordered GROUP BY comes back
                # ordered by group key (NULLs first). Match it or every differential case fails
                # on row order alone.
                order.sort(key=_group_sort_key)

            for key in order:
                group = groups[key]
                if node.having is not None and not truthy(self._eval(node.having, None, group[0], group)):
                    continue
                rows.append(tuple(self._project(item, group) for item in projection))
        else:
            for env in envs:
                rows.append(tuple(self._project(item, [env]) for item in projection))

        if node.distinct:
            rows = _distinct(rows)

        self.last_plan = {
            "table": node.table,
            "tables": [source["alias"] for source in source_list],
            "access": "nested-loop",
            "sources": [{"table": source["alias"], "rows": len(source["rows"])}
                        for source in source_list],
            "rows_examined": examined,
            "rows_matched": len(rows),
            "reason": "join" if node.joins else "aggregate",
            "index": None,
            "candidates": [],
        }
        return items, rows

    def _project(self, item, group):
        env = group[0] if group else {}
        if isinstance(item, ast.Star):
            raise ExecError("SELECT * is expanded before projection")
        return self._eval(item.expr, None, env, group)

    def _aggregate(self, node, group):
        if node.star:
            return len(group)
        values = [self._eval(node.arg, None, env, group) for env in group]
        values = [value for value in values if value is not None]
        if getattr(node, "distinct", False):
            seen, unique = set(), []
            for value in values:
                key = (type(value).__name__, value)
                if key not in seen:
                    seen.add(key)
                    unique.append(value)
            values = unique
        if node.name == "COUNT":
            return len(values)
        if not values:
            return None
        if node.name == "SUM":
            return sum(values)
        if node.name == "AVG":
            return sum(values) / len(values)
        if node.name == "MIN":
            return min(values)
        if node.name == "MAX":
            return max(values)
        raise ExecError(f"unsupported aggregate {node.name}")

    def _scalar_subquery(self, select):
        saved = self.last_plan
        try:
            _, rows = self._select(select)
        finally:
            self.last_plan = saved
        if not rows:
            return None
        return rows[0][0]

    def _in_subquery(self, node, env, group):
        value = self._eval(node.expr, None, env, group)
        saved = self.last_plan
        try:
            _, rows = self._select(node.select)
        finally:
            self.last_plan = saved
        values = {row[0] for row in rows}
        if value is None:
            return None
        found = value in values
        if not found and None in values:
            return None
        return (not found) if node.negated else found

    # -- expressions ---------------------------------------------------------
    def _eval(self, node, table, env, group=None):
        if isinstance(node, ast.Literal):
            return node.value
        if isinstance(node, ast.Subquery):
            return self._scalar_subquery(node.select)
        if isinstance(node, ast.InSubquery):
            return self._in_subquery(node, env, group)
        if isinstance(node, ast.FuncCall):
            if node.name not in self.AGGREGATES:
                raise ExecError(f"no such function: {node.name}")
            if group is None:
                raise ExecError(f"misuse of aggregate function {node.name}()")
            return self._aggregate(node, group)
        if isinstance(node, ast.ColumnRef):
            key = (f"{node.table.lower()}.{node.name.lower()}" if node.table
                   else node.name.lower())
            if env is None or key not in env:
                # SQLite adds the hint only for double-quoted identifiers, and prints the
                # quotes back; bracket/backtick forms lose their quotes in the message.
                display = f"{node.table}.{node.name}" if node.table else node.name
                if node.quote == '"' and not node.table:
                    raise ExecError(
                        f'no such column: "{node.name}" - should this be a string literal in single-quotes?'
                    )
                raise ExecError(f"no such column: {display}")
            return env[key]
        if isinstance(node, ast.Case):
            if node.operand is not None:
                base = self._eval(node.operand, table, env, group)
                for when, then in node.whens:
                    if sql_is(base, self._eval(when, table, env, group)):
                        return self._eval(then, table, env, group)
            else:
                for when, then in node.whens:
                    if truthy(self._eval(when, table, env, group)):
                        return self._eval(then, table, env, group)
            return self._eval(node.else_expr, table, env, group) if node.else_expr is not None else None

        if isinstance(node, ast.Cast):
            return cast_value(self._eval(node.expr, table, env, group), node.type_name)

        if isinstance(node, ast.Between):
            value = self._eval(node.expr, table, env, group)
            low = self._eval(node.low, table, env, group)
            high = self._eval(node.high, table, env, group)
            if value is None or low is None or high is None:
                return None
            inside = sql_compare(value, low, ">=") and sql_compare(value, high, "<=")
            return (not inside) if node.negated else inside

        if isinstance(node, ast.BinaryOp):
            if node.op in ("+", "-", "*", "/", "%", "||"):
                return arithmetic(node.op, self._eval(node.left, table, env, group),
                                  self._eval(node.right, table, env, group))
            if node.op in ("IS", "IS NOT"):
                result = sql_is(self._eval(node.left, table, env, group),
                                self._eval(node.right, table, env, group))
                return (not result) if node.op == "IS NOT" else result
            if node.op == "AND":
                return sql_and(self._eval(node.left, table, env, group),
                               self._eval(node.right, table, env, group))
            if node.op == "OR":
                return sql_or(self._eval(node.left, table, env, group),
                              self._eval(node.right, table, env, group))
            return sql_compare(self._eval(node.left, table, env, group),
                               self._eval(node.right, table, env, group), node.op)
        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand, table, env, group)
            if node.op == "NOT":
                return None if value is None else not truthy(value)
            if node.op == "-":
                return None if value is None else -value
            if node.op == "+":
                return value
        if isinstance(node, ast.IsNull):
            value = self._eval(node.expr, table, env, group)
            result = value is None
            return (not result) if node.negated else result
        raise ExecError(f"cannot evaluate {type(node).__name__}")


def _group_sort_key(key):
    """SQLite's storage-class order: NULLs first, then numbers, text, blobs."""
    return tuple((0, 0) if value is None else (_RANK.get(type(value), 9), value) for value in key)


def _heading(item):
    """SQLite names a plain column reference after the column; anything else after its text."""
    if isinstance(item.expr, ast.ColumnRef):
        return item.expr.name
    return item.text or _describe(item.expr)


def _distinct(rows):
    seen, unique = set(), []
    for row in rows:
        if row not in seen:
            seen.add(row)
            unique.append(row)
    return unique


def _describe(expr):
    """Column heading SQLite would use for an unaliased expression."""
    if isinstance(expr, ast.ColumnRef):
        return expr.name
    if isinstance(expr, ast.Literal):
        return str(expr.value)
    return "?"
