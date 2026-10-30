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
import string

from .sql import ast
from .storage.disk import DiskStorage
from .storage.pager import StorageError

# SQLite's storage-class ordering, used when comparing values of different types.
_RANK = {type(None): 0, int: 1, float: 1, str: 2, bytes: 3}


class ExecError(Exception):
    """Mirrors sqlite3.Error: message text should match SQLite's own where one exists."""


def _rank(value):
    return _RANK[type(value)]


def sql_compare(left, right, op, nocase=False):
    """Three-valued comparison: NULL in either side yields None, as in SQL."""
    if left is None or right is None:
        return None
    if nocase and isinstance(left, str) and isinstance(right, str):
        left, right = left.lower(), right.lower()
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


def affinity_of(type_name):
    """SQLite's affinity rules, in its own order: INT first, then CHAR/CLOB/TEXT, then BLOB,
    then REAL/FLOA/DOUB, and NUMERIC for everything else."""
    if not type_name:
        return "BLOB"
    name = type_name.upper()
    if "INT" in name:
        return "INTEGER"
    if "CHAR" in name or "CLOB" in name or "TEXT" in name:
        return "TEXT"
    if "BLOB" in name or name.strip() == "":
        return "BLOB"
    if "REAL" in name or "FLOA" in name or "DOUB" in name:
        return "REAL"
    return "NUMERIC"


def _numeric_prefix(text):
    match = re.match(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", text)
    if not match:
        return None
    body = match.group(1)
    try:
        if re.fullmatch(r"[+-]?\d+", body):
            return int(body)
        return float(body)
    except ValueError:
        return None


def apply_affinity(value, affinity):
    """Column affinity applied on write, which is what makes typeof('5') report integer."""
    if value is None or affinity == "BLOB":
        return value
    if affinity == "TEXT":
        return value if isinstance(value, str) else text_of(value)
    if affinity in ("INTEGER", "NUMERIC"):
        if isinstance(value, str):
            # Only a losslessly convertible string is coerced: '12abc' stays text in a column,
            # even though CAST('12abc' AS INTEGER) is 12.
            stripped = value.strip()
            if re.fullmatch(r"[+-]?\d+", stripped):
                return int(stripped)
            if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?", stripped):
                number = float(stripped)
                return int(number) if number.is_integer() else number
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    if affinity == "REAL":
        if isinstance(value, str):
            stripped = value.strip()
            if re.fullmatch(r"[+-]?\d+", stripped) or \
                    re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?", stripped):
                return float(stripped)
            return value          # 'x' stays text even in a REAL column
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return value
    return value


def text_of(value):
    return to_text(value) if value is not None else ""


def call_function(name, args):
    """Scalar built-ins (milestone 11). NULL handling follows SQLite, which is not uniform."""
    if name == "COALESCE":
        for value in args:
            if value is not None:
                return value
        return None
    if name == "IFNULL":
        return args[0] if args[0] is not None else (args[1] if len(args) > 1 else None)
    if name == "NULLIF":
        if len(args) < 2:
            raise ExecError("nullif() needs two arguments")
        return None if args[0] == args[1] else args[0]
    if name in ("MIN", "MAX"):
        # Scalar min/max: any NULL argument makes the result NULL (SQLite's rule, and the
        # opposite of the filtering behaviour the aggregate form uses).
        if any(value is None for value in args):
            return None
        return min(args) if name == "MIN" else max(args)
    if name == "TYPEOF":
        value = args[0]
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "integer"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "real"
        if isinstance(value, bytes):
            return "blob"
        return "text"

    # Everything below treats NULL as NULL, like SQLite does for these functions.
    if any(value is None for value in args):
        return None

    if name == "ABS":
        value = to_number(args[0])
        return abs(value)
    if name == "LENGTH":
        value = args[0]
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, str):
            return len(value)
        return len(to_text(value))
    if name == "UPPER":
        # SQLite's case mapping is ASCII-only: 'héllo' becomes 'HéLLO', not 'HÉLLO'.
        return text_of(args[0]).translate(str.maketrans(string.ascii_lowercase, string.ascii_uppercase))
    if name == "LOWER":
        return text_of(args[0]).translate(str.maketrans(string.ascii_uppercase, string.ascii_lowercase))
    if name == "SUBSTR" or name == "SUBSTRING":
        # SQLite's own rule set: 1-based, negative counts from the end, and Y=0 starts one
        # character before the string (so substr('abcdef',0,3) is 'ab', not 'abc').
        text = text_of(args[0])
        start = int(to_number(args[1]))
        if start > 0:
            first = start
        elif start < 0:
            first = len(text) + start + 1
        else:
            first = 0
        if len(args) >= 3:
            end = first + int(to_number(args[2]))
        else:
            end = len(text) + 1
        begin = max(1, first)
        finish = min(len(text) + 1, end)
        return text[begin - 1:max(begin - 1, finish - 1)]
    if name in ("TRIM", "LTRIM", "RTRIM"):
        text = text_of(args[0])
        characters = text_of(args[1]) if len(args) > 1 else " "
        if name == "TRIM":
            return text.strip(characters)
        if name == "LTRIM":
            return text.lstrip(characters)
        return text.rstrip(characters)
    if name == "REPLACE":
        if len(args) < 3:
            raise ExecError("replace() needs three arguments")
        haystack, needle, replacement = text_of(args[0]), text_of(args[1]), text_of(args[2])
        if needle == "":
            return haystack           # SQLite leaves the string alone for an empty pattern
        return haystack.replace(needle, replacement)
    if name == "ROUND":
        value = float(to_number(args[0]))
        places = int(to_number(args[1])) if len(args) > 1 else 0
        # SQLite rounds half away from zero, Python rounds half to even.
        from decimal import Decimal, ROUND_HALF_UP
        quantised = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        result = float(quantised)
        return result if places > 0 else int(result)
    if name in ("SQRT", "POW", "POWER", "FLOOR", "CEIL", "CEILING", "LOG", "LOG10", "LOG2",
                "EXP", "PI", "MOD", "TRUNC"):
        import math
        if name == "PI":
            return math.pi
        numbers = [float(to_number(value)) for value in args]
        if name == "SQRT":
            return math.sqrt(numbers[0]) if numbers[0] >= 0 else None
        if name in ("POW", "POWER"):
            return math.pow(numbers[0], numbers[1])
        if name == "FLOOR":
            return int(math.floor(numbers[0]))
        if name in ("CEIL", "CEILING"):
            return int(math.ceil(numbers[0]))
        if name == "LOG":
            # SQLite's log(X) is base 10; log(X,Y) is base Y.
            if len(numbers) == 1:
                return math.log10(numbers[0]) if numbers[0] > 0 else None
            return math.log(numbers[0], numbers[1]) if numbers[0] > 0 else None
        if name == "LOG10":
            return math.log10(numbers[0]) if numbers[0] > 0 else None
        if name == "LOG2":
            return math.log2(numbers[0]) if numbers[0] > 0 else None
        if name == "EXP":
            return math.exp(numbers[0])
        if name == "MOD":
            return numbers[0] % numbers[1] if numbers[1] else None
        if name == "TRUNC":
            return int(numbers[0])

    if name == "INSTR":
        return text_of(args[0]).find(text_of(args[1])) + 1
    if name in ("DATE", "TIME", "DATETIME", "JULIANDAY", "STRFTIME", "UNIXEPOCH"):
        return datetime_function(name, args)
    raise ExecError(f"no such function: {name.lower()}")


def _parse_datetime(value):
    import datetime as _dt
    text = text_of(value).strip()
    if not text:
        return None
    if text.lower() == "now":
        return _dt.datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def apply_modifiers(moment, modifiers):
    """SQLite date modifiers: ±N days/hours/minutes/seconds/months/years, start of X, unixepoch."""
    import datetime as _dt
    import calendar
    for modifier in modifiers:
        text = text_of(modifier).strip().lower()
        if text in ("", "localtime", "utc"):
            continue
        if text.startswith("start of "):
            unit = text[len("start of "):]
            if unit == "day":
                moment = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            elif unit == "month":
                moment = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            elif unit == "year":
                moment = moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            else:
                return None
            continue
        match = re.match(r"^([+-]?\d+)\s+(second|minute|hour|day|month|year)s?$", text)
        if not match:
            return None
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "second":
            moment += _dt.timedelta(seconds=amount)
        elif unit == "minute":
            moment += _dt.timedelta(minutes=amount)
        elif unit == "hour":
            moment += _dt.timedelta(hours=amount)
        elif unit == "day":
            moment += _dt.timedelta(days=amount)
        elif unit == "month":
            # SQLite normalises overflow instead of clamping: 2024-01-31 +1 month is 2024-03-02.
            month_index = (moment.year * 12 + moment.month - 1) + amount
            year, month = divmod(month_index, 12)
            month += 1
            base = moment.replace(year=year, month=month, day=1)
            moment = base + _dt.timedelta(days=moment.day - 1)
        elif unit == "year":
            base = moment.replace(year=moment.year + amount, day=1)
            moment = base + _dt.timedelta(days=moment.day - 1)
    return moment


def datetime_function(name, args):
    modifiers = args[1:] if name != "STRFTIME" else args[2:]
    if name == "STRFTIME":
        if len(args) < 2:
            raise ExecError("strftime() needs a format and a time value")
        moment = _parse_datetime(args[1])
        if moment is None:
            return None
        moment = apply_modifiers(moment, modifiers)
        if moment is None:
            return None
        return moment.strftime(text_of(args[0]))
    moment = _parse_datetime(args[0])
    if moment is None:
        return None
    moment = apply_modifiers(moment, modifiers)
    if moment is None:
        return None
    if name == "DATE":
        return moment.strftime("%Y-%m-%d")
    if name == "TIME":
        return moment.strftime("%H:%M:%S")
    if name == "DATETIME":
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    if name == "JULIANDAY":
        import datetime as _dt
        delta = moment - _dt.datetime(1970, 1, 1)
        return 2440587.5 + delta.total_seconds() / 86400.0
    if name == "UNIXEPOCH":
        import datetime as _dt
        return int((moment - _dt.datetime(1970, 1, 1)).total_seconds())
    raise ExecError(f"no such function: {name.lower()}")


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
    __slots__ = ("name", "type_name", "not_null", "unique", "primary_key", "default", "check",
                 "check_text", "ref_table", "ref_column", "affinity", "collation")

    def __init__(self, definition: ast.ColumnDef):
        self.name = definition.name
        self.type_name = definition.type_name
        self.not_null = False
        self.unique = False
        self.primary_key = False
        self.default = None
        self.check = None
        self.check_text = None
        self.ref_table = None
        self.ref_column = None
        self.affinity = affinity_of(definition.type_name)
        self.collation = None
        for constraint in definition.constraints:
            if constraint.kind == "COLLATE":
                self.collation = (constraint.text or "").upper()
            elif constraint.kind == "REFERENCES":
                self.ref_table = constraint.ref_table
                self.ref_column = constraint.ref_column
            elif constraint.kind == "PRIMARY KEY":
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
                self.default = constraint.expr
            elif constraint.kind == "CHECK":
                self.check = constraint.expr
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
        self.views = {}
        self.triggers = {}
        self.trigger_env = None
        self._in_trigger = False
        self.foreign_keys = False
        self._sql = None
        self.last_plan = None
        self.tx_snapshot = None
        if self.storage is not None:
            self._load_schema()

    def _load_schema(self):
        from .sql.parser import parse
        for entry in self.storage.catalog.values():
            statement = parse(entry["sql"])
            if entry.get("kind") == "view":
                self.views[entry["name"].lower()] = statement
                continue
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
        if isinstance(statement, ast.Compound):
            return self._compound(statement)
        if isinstance(statement, ast.Pragma):
            return self._pragma(statement)
        if isinstance(statement, ast.CreateTrigger):
            return self._create_trigger(statement)
        if isinstance(statement, ast.DropTrigger):
            return self._drop_trigger(statement)
        if isinstance(statement, ast.AlterTable):
            return self._alter(statement)
        if isinstance(statement, ast.Update):
            return self._update(statement)
        if isinstance(statement, ast.Delete):
            return self._delete(statement)
        if isinstance(statement, ast.CreateView):
            return self._create_view(statement)
        if isinstance(statement, ast.DropView):
            return self._drop_view(statement)
        if isinstance(statement, ast.With):
            return self._with(statement)
        raise ExecError(f"unsupported statement {type(statement).__name__}")

    def run(self, sql):
        from .sql.parser import parse
        self._sql = sql
        return self.execute(parse(sql), autocommit=True)

    # -- CREATE --------------------------------------------------------------
    def _create(self, node: ast.CreateTable):
        key = node.name.lower()
        if key in self.tables or key in self.views:
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
        index_names = {column.name.lower() for column in columns}
        for constraint in getattr(node, "table_constraints", []):
            if constraint.kind in ("PRIMARY KEY", "UNIQUE"):
                for name in constraint.columns:
                    if name.lower() not in index_names:
                        raise ExecError(f"no such column: {name}")
            elif constraint.kind == "FOREIGN KEY":
                for position, name in enumerate(constraint.columns):
                    if name.lower() not in index_names:
                        raise ExecError(f"no such column: {name}")
                    target = columns[index_names and [c.name.lower() for c in columns].index(name.lower())]
                    target.ref_table = constraint.ref_table
                    if position < len(constraint.ref_columns):
                        target.ref_column = constraint.ref_columns[position]
        if self.storage is not None:
            entry = self.storage.create_table(node.name, node.sql or self._sql or f"CREATE TABLE {node.name}")
            self.tables[key] = Table(node.name, columns, entry)
        else:
            self.tables[key] = Table(node.name, columns)
        table = self.tables[key]
        for constraint in getattr(node, "table_constraints", []):
            if constraint.kind in ("PRIMARY KEY", "UNIQUE"):
                positions = [table.index_of[name.lower()] for name in constraint.columns]
                name = f"sqlite_autoindex_{node.name}_{len(table.indexes) + 1}"
                index = Index(name, table.name, positions, list(constraint.columns), True)
                table.indexes[name.lower()] = index
                if self.storage is not None:
                    self._persist_indexes(table)
            elif constraint.kind == "CHECK":
                table.checks = getattr(table, "checks", [])
                table.checks.append((constraint.expr, constraint.text))
        return None

    def table(self, name):
        key = name.lower()
        if key in self.tables:
            return self.tables[key]
        if key in self.views:
            return self._materialise_view(self.views[key])
        raise ExecError(f"no such table: {name}")

    # -- views ---------------------------------------------------------------
    def _create_view(self, node: ast.CreateView):
        key = node.name.lower()
        if key in self.tables or key in self.views:
            if node.if_not_exists:
                return None
            raise ExecError(f"view {node.name} already exists")
        self.views[key] = node
        if self.storage is not None:
            self.storage.create_view(node.name, node.sql or self._sql or f"CREATE VIEW {node.name}")
        return None

    def _drop_view(self, node):
        key = node.name.lower()
        if key in self.views:
            del self.views[key]
            if self.storage is not None:
                self.storage.drop_entry(node.name)
            return None
        if node.if_exists:
            return None
        raise ExecError(f"no such view: {node.name}")

    def _materialise_view(self, view: ast.CreateView):
        columns, rows = self._select_node(view.select)
        names = view.columns or columns
        table = Table(view.name, [Column(ast.ColumnDef(name=name)) for name in names])
        table.rows = [tuple(row) for row in rows]
        return table

    def _updatable_view(self, name):
        """A view is auto-updatable here when it is a plain projection of one base table."""
        view = self.views.get(name.lower())
        if view is None or not isinstance(view.select, ast.Select):
            return None
        select = view.select
        if select.group_by or select.distinct or select.joins or select.where is not None \
                or select.limit is not None or select.base_select is not None or select.table is None:
            return None
        base = self.tables.get(select.table.lower())
        if base is None:
            return None
        columns = []
        for item in select.items:
            if isinstance(item, ast.Star):
                columns.extend(base.column_names)
            elif isinstance(item.expr, ast.ColumnRef):
                columns.append(item.expr.name)
            else:
                return None
        return base, columns

    # -- CTEs ----------------------------------------------------------------
    def _with(self, node: ast.With):
        saved = {}
        try:
            for cte in node.ctes:
                table = self._recursive_cte(cte) if cte.recursive and _references(cte.select, cte.name) \
                    else self._materialise_cte(cte)
                key = cte.name.lower()
                saved[key] = self.tables.get(key)
                self.tables[key] = table
            return self._select_node(node.body)
        finally:
            for key, previous in saved.items():
                if previous is None:
                    self.tables.pop(key, None)
                else:
                    self.tables[key] = previous

    def _materialise_cte(self, cte):
        columns, rows = self._select_node(cte.select)
        names = cte.columns or columns
        table = Table(cte.name, [Column(ast.ColumnDef(name=name)) for name in names])
        table.rows = [tuple(row) for row in rows]
        return table

    def _recursive_cte(self, cte):
        body = cte.select
        if not isinstance(body, ast.Compound) or body.op not in ("UNION", "UNION ALL"):
            raise ExecError("recursive CTE must use UNION or UNION ALL")
        columns, base_rows = self._select_node(body.left)
        names = cte.columns or columns
        accumulated = []
        seen = set()
        for row in base_rows:
            marker = tuple(_value_key(value) for value in row)
            if body.op == "UNION" and marker in seen:
                continue          # UNION dedupes the seed rows too, not just the recursive ones
            seen.add(marker)
            accumulated.append(tuple(row))
        working = list(accumulated)
        for _ in range(1000):
            table = Table(cte.name, [Column(ast.ColumnDef(name=name)) for name in names])
            table.rows = working
            self.tables[cte.name.lower()] = table
            _, rows = self._select_node(body.right)
            fresh = []
            for row in rows:
                marker = tuple(_value_key(value) for value in row)
                if body.op == "UNION ALL":
                    fresh.append(tuple(row))
                elif marker not in seen:
                    seen.add(marker)
                    fresh.append(tuple(row))
            if not fresh:
                break
            accumulated.extend(fresh)
            working = fresh
        else:
            raise ExecError("recursive CTE exceeded 1000 iterations")
        table = Table(cte.name, [Column(ast.ColumnDef(name=name)) for name in names])
        table.rows = accumulated
        return table

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
        if node.table.lower() in self.views and node.table.lower() not in self.tables:
            # SQLite views are read-only unless they have INSTEAD OF triggers (not in scope yet).
            raise ExecError(f"cannot modify {node.table} because it is a view")
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
                values[i] = apply_affinity(values[i], column.affinity)
                if values[i] is None and column.not_null:
                    raise ExecError(f"NOT NULL constraint failed: {table.name}.{column.name}")
                if values[i] is None:
                    continue
                if column.check is not None and not truthy(self._eval(column.check, table, dict(zip(table.column_names, values)))):
                    raise ExecError(f"CHECK constraint failed: {column.check_text}")
                if column.unique:
                    for existing in self._rows(table):
                        same = existing[i]
                        if (column.collation == "NOCASE" and isinstance(same, str)
                                and isinstance(values[i], str)):
                            same = same.lower()
                            compared = values[i].lower()
                        else:
                            compared = values[i]
                        if same == compared:
                            raise ExecError(f"UNIQUE constraint failed: {table.name}.{column.name}")
            for check_expr, check_text in getattr(table, "checks", []):
                if not truthy(self._eval(check_expr, table, dict(zip(table.column_names, values)))):
                    raise ExecError(f"CHECK constraint failed: {check_text}")
            # Unique indexes are checked before the row lands, so a rejected insert leaves
            # neither a row nor a dangling index entry.
            for index in table.indexes.values():
                index.check_unique(values)
            violation = self._foreign_key_violation(table, values)
            if violation:
                raise ExecError(violation)
            self._fire(table, "INSERT", "BEFORE", None, values)
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
            self._fire(table, "INSERT", "AFTER", None, values)
        return len(node.rows)

    # -- PRAGMA / foreign keys ----------------------------------------------
    def _pragma(self, node: ast.Pragma):
        name = node.name.lower()
        if name == "foreign_keys":
            if node.value is not None:
                self.foreign_keys = truthy(self._eval(node.value, None, {}))
                return (["foreign_keys"], [])
            return (["foreign_keys"], [(1 if self.foreign_keys else 0,)])
        raise ExecError(f"no such pragma: {node.name}")

    def _foreign_key_violation(self, table, values):
        """Only columns declared REFERENCES are checked; enforcement is off until the pragma
        turns it on, which is the reference's surprising default."""
        if not self.foreign_keys:
            return None
        for position, column in enumerate(table.columns):
            if not column.ref_table:
                continue
            value = values[position]
            if value is None:
                continue
            parent = self.tables.get(column.ref_table.lower())
            if parent is None:
                return None
            ref_name = (column.ref_column or "rowid").lower()
            ref_position = parent.index_of.get(ref_name)
            if ref_position is None:
                return None
            if not any(row[ref_position] == value for row in self._rows(parent)):
                return "FOREIGN KEY constraint failed"
        return None

    def _check_no_action(self, table, row):
        if not self.foreign_keys:
            return
        for other, position, ref_column in self._dependent_rows(table):
            key_position = table.index_of.get((ref_column or "rowid").lower())
            if key_position is None:
                continue
            key = row[key_position]
            if key is None:
                continue
            if any(other_row[position] == key for other_row in self._rows(other)):
                raise ExecError("FOREIGN KEY constraint failed")

    def _dependent_rows(self, table):
        """Tables holding a REFERENCES to `table`, for NO ACTION checks on delete/update."""
        dependents = []
        for other in self.tables.values():
            for position, column in enumerate(other.columns):
                if column.ref_table and column.ref_table.lower() == table.name.lower():
                    dependents.append((other, position, column.ref_column))
        return dependents

    # -- triggers ------------------------------------------------------------
    def _create_trigger(self, node: ast.CreateTrigger):
        key = node.name.lower()
        if key in self.triggers:
            if node.if_not_exists:
                return None
            raise ExecError(f"trigger {node.name} already exists")
        self.table(node.table)
        self.triggers[key] = node
        return None

    def _drop_trigger(self, node: ast.DropTrigger):
        key = node.name.lower()
        if key in self.triggers:
            del self.triggers[key]
            return None
        if node.if_exists:
            return None
        raise ExecError(f"no such trigger: {node.name}")

    def _fire(self, table, event, timing, old_values=None, new_values=None):
        if self._in_trigger:
            return          # SQLite does not fire triggers recursively by default
        for trigger in list(self.triggers.values()):
            if (trigger.table.lower() != table.name.lower() or trigger.event != event
                    or trigger.timing != timing):
                continue
            env = {}
            if new_values is not None:
                for column, value in zip(table.columns, new_values):
                    env[f"new.{column.name.lower()}"] = value
                    env[column.name.lower()] = value
            if old_values is not None:
                for column, value in zip(table.columns, old_values):
                    env[f"old.{column.name.lower()}"] = value
                    env.setdefault(column.name.lower(), value)
            if trigger.when is not None and not truthy(self._eval(trigger.when, table, env)):
                continue
            previous = self.trigger_env
            self.trigger_env = env
            self._in_trigger = True
            try:
                for statement in trigger.statements:
                    self._execute(statement)
            finally:
                self._in_trigger = False
                self.trigger_env = previous

    # -- ALTER TABLE ---------------------------------------------------------
    def _alter(self, node: ast.AlterTable):
        table = self.table(node.table)
        if node.action == "add":
            column = Column(node.column)
            if column.name.lower() in table.index_of:
                raise ExecError(f"duplicate column name: {column.name}")
            if column.not_null and column.default is None:
                raise ExecError(f"Cannot add a NOT NULL column with default value NULL")
            table.columns.append(column)
            table.index_of[column.name.lower()] = len(table.columns) - 1
            default = self._eval(column.default, table, {}) if column.default is not None else None
            if table.on_disk:
                rows = [(rowid, tuple(row) + (default,)) for rowid, row in self._rows_with_ids(table)]
                self.storage.rewrite_rows(table.entry, rows)
            else:
                table.rows = [tuple(row) + (default,) for row in table.rows]
        elif node.action == "rename_table":
            new = node.new_name
            if new.lower() in self.tables or new.lower() in self.views:
                raise ExecError(f"table {new} already exists")
            del self.tables[table.name.lower()]
            if table.on_disk:
                self.storage.drop_entry(table.name)
                entry = self.storage.create_table(new, self._render_create(table, new))
                table.entry = entry
            table.name = new
            self.tables[new.lower()] = table
        elif node.action == "rename_column":
            position = table.index_of.get(node.old_name.lower())
            if position is None:
                raise ExecError(f"no such column: {node.old_name}")
            table.columns[position].name = node.new_name
            table.index_of.pop(node.old_name.lower(), None)
            table.index_of[node.new_name.lower()] = position
        elif node.action == "drop_column":
            position = table.index_of.get(node.old_name.lower())
            if position is None:
                raise ExecError(f"no such column: {node.old_name}")
            if len(table.columns) == 1:
                raise ExecError(f"cannot drop column \"{node.old_name}\": no other columns exist")
            del table.columns[position]
            table.index_of = {c.name.lower(): i for i, c in enumerate(table.columns)}
            if table.on_disk:
                rows = [(rowid, tuple(v for i, v in enumerate(row) if i != position))
                        for rowid, row in self._rows_with_ids(table)]
                self.storage.rewrite_rows(table.entry, rows)
            else:
                table.rows = [tuple(v for i, v in enumerate(row) if i != position)
                              for row in table.rows]
        if table.on_disk and node.action != "rename_table":
            table.entry["sql"] = self._render_create(table, table.name)
            self.storage.save_catalog_entry(table.entry)
        return None

    def _render_create(self, table, name):
        parts = []
        for column in table.columns:
            text = column.name
            if column.type_name:
                text += " " + column.type_name
            if column.not_null:
                text += " NOT NULL"
            if column.unique:
                text += " UNIQUE"
            if column.primary_key:
                text += " PRIMARY KEY"
            if column.default is not None:
                text += " DEFAULT " + _render_expression(column.default)
            if column.check_text:
                text += " CHECK (" + column.check_text + ")"
            parts.append(text)
        return f"CREATE TABLE {name} (" + ", ".join(parts) + ")"

    # -- UPDATE / DELETE -----------------------------------------------------
    def _collation(self, node, table):
        if isinstance(node, ast.Collate):
            return node.name.upper()
        if isinstance(node, ast.ColumnRef) and table is not None:
            position = table.index_of.get(node.name.lower())
            if position is not None:
                return table.columns[position].collation
        return None

    def _order_key(self, value, collation):
        if collation == "NOCASE" and isinstance(value, str):
            return _order_sort_key(value.lower())
        return _order_sort_key(value)

    def _row_env(self, table, row):
        env = {}
        for column, value in zip(table.columns, row):
            env[column.name.lower()] = value
            env[f"{table.name.lower()}.{column.name.lower()}"] = value
        return env

    def _modified_table(self, node):
        if node.table.lower() in self.views and node.table.lower() not in self.tables:
            raise ExecError(f"cannot modify {node.table} because it is a view")
        return self.table(node.table)

    def _update(self, node: ast.Update):
        table = self._modified_table(node)
        positions = []
        for column, _ in node.assignments:
            position = table.index_of.get(column.lower())
            if position is None:
                raise ExecError(f"no such column: {column}")
            positions.append(position)
        if table.on_disk:
            rows = list(self._rows_with_ids(table))
            changed = 0
            rewritten = []
            for rowid, row in rows:
                env = self._row_env(table, row)
                if node.where is not None and not truthy(self._eval(node.where, table, env)):
                    rewritten.append((rowid, row))
                    continue
                values = list(row)
                for (_, expression), position in zip(node.assignments, positions):
                    values[position] = self._eval(expression, table, env)
                rewritten.append((rowid, tuple(values)))
                changed += 1
            if changed:
                self.storage.rewrite_rows(table.entry, rewritten)
            self._reindex(table)
            return changed

        changed = 0
        for index, row in enumerate(list(table.rows)):
            env = self._row_env(table, row)
            if node.where is not None and not truthy(self._eval(node.where, table, env)):
                continue
            values = list(row)
            # All assignments read the OLD row, so `SET a = b, b = a` swaps rather than copies.
            for (_, expression), position in zip(node.assignments, positions):
                values[position] = apply_affinity(self._eval(expression, table, env),
                                                  table.columns[position].affinity)
            self._fire(table, "UPDATE", "BEFORE", row, values)
            table.rows[index] = tuple(values)
            self._fire(table, "UPDATE", "AFTER", row, values)
            changed += 1
        self._reindex(table)
        return changed

    def _delete(self, node: ast.Delete):
        table = self._modified_table(node)
        if table.on_disk:
            kept = []
            changed = 0
            for rowid, row in self._rows_with_ids(table):
                env = self._row_env(table, row)
                if node.where is not None and not truthy(self._eval(node.where, table, env)):
                    kept.append((rowid, row))
                else:
                    self._check_no_action(table, row)
                    changed += 1
            if changed:
                self.storage.rewrite_rows(table.entry, kept)
            self._reindex(table)
            return changed
        kept = []
        changed = 0
        for row in table.rows:
            env = self._row_env(table, row)
            if node.where is not None and not truthy(self._eval(node.where, table, env)):
                kept.append(row)
            else:
                self._check_no_action(table, row)
                self._fire(table, "DELETE", "BEFORE", row, None)
                self._fire(table, "DELETE", "AFTER", row, None)
                changed += 1
        table.rows = kept
        self._reindex(table)
        return changed

    def _reindex(self, table):
        for index in table.indexes.values():
            index.map = {}
            for rowid, values in enumerate(table.rows, start=1):
                index.add(rowid, values)

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
            return node.name in self.AGGREGATES and len(node.args or []) <= 1
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
            columns, rows = self._select_node(node.base_select)
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
                columns, rows = self._select_node(join.select)
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

    def _window_values(self, window, prepared, table):
        """Compute one window function over the prepared rows, in original row order."""
        count = len(prepared)
        if count == 0:
            return []
        partitions = {}
        for index, (env, _) in enumerate(prepared):
            key = tuple(_value_key(self._eval(expression, table, env))
                        for expression in window.partition_by)
            partitions.setdefault(key, []).append(index)

        values = [None] * count
        for indices in partitions.values():
            ordered = list(indices)
            for expression, descending in reversed(window.order_by):
                ordered.sort(key=lambda i, e=expression: _order_sort_key(
                    self._eval(e, table, prepared[i][0])), reverse=descending)
            order_keys = {
                index: tuple(_order_sort_key(self._eval(expression, table, prepared[index][0]))
                             for expression, _ in window.order_by)
                for index in ordered
            }
            rank = 0
            dense = 0
            previous_key = None
            for position, index in enumerate(ordered):
                name = window.name
                if name in ("RANK", "DENSE_RANK"):
                    current_key = order_keys[index]
                    if position == 0 or current_key != previous_key:
                        rank = position + 1
                        dense += 1
                        previous_key = current_key
                    values[index] = rank if name == "RANK" else dense
                elif name == "ROW_NUMBER":
                    values[index] = position + 1
                elif name == "LAG":
                    offset = int(to_number(self._eval(window.args[1], table, prepared[index][0]))) \
                        if len(window.args) > 1 else 1
                    default = self._eval(window.args[2], table, prepared[index][0]) \
                        if len(window.args) > 2 else None
                    source = position - offset
                    values[index] = (self._eval(window.args[0], table, prepared[ordered[source]][0])
                                     if 0 <= source < len(ordered) else default)
                else:
                    start, end = self._frame_bounds(window, position, len(ordered))
                    values[index] = self._window_aggregate(window, prepared, table,
                                                           ordered[start:end + 1])
        return values

    def _frame_bounds(self, window, position, length):
        if window.frame is None:
            if window.order_by:
                return 0, position
            return 0, length - 1
        _, start, end = window.frame

        def resolve(bound, default):
            kind, value = bound
            if kind == "unbounded":
                return 0 if value == "preceding" else length - 1
            if kind == "current":
                return position
            offset = int(to_number(self._eval(value, None, {})))
            return position - offset if value is not None and kind == "preceding" else position + offset

        first = resolve(start, 0)
        last = resolve(end, length - 1)
        return max(0, first), min(length - 1, last)

    def _window_aggregate(self, window, prepared, table, indices):
        name = window.name
        if name == "COUNT" and (window.args == [] or window.arg is None):
            return len(indices)
        values = [self._eval(window.args[0], table, prepared[index][0]) for index in indices]
        if name == "COUNT":
            return sum(1 for value in values if value is not None)
        values = [value for value in values if value is not None]
        if name == "SUM":
            return sum(values) if values else None
        if name == "AVG":
            return (sum(values) / len(values)) if values else None
        if name == "MIN":
            return min(values) if values else None
        if name == "MAX":
            return max(values) if values else None
        raise ExecError(f"no such window function: {name.lower()}")

    def _compound(self, node: ast.Compound):
        left_items, left_rows = self._select_node(node.left)
        right_items, right_rows = self._select_node(node.right)
        if len(left_items) != len(right_items):
            raise ExecError(f"SELECTs to the left and right of {node.op} "
                            "do not have the same number of result columns")

        def key(row):
            return tuple(_value_key(value) for value in row)

        if node.op == "UNION ALL":
            rows = list(left_rows) + list(right_rows)
        elif node.op == "UNION":
            rows, seen = [], set()
            for row in list(left_rows) + list(right_rows):
                marker = key(row)
                if marker not in seen:
                    seen.add(marker)
                    rows.append(row)
        elif node.op == "INTERSECT":
            right_keys = {key(row) for row in right_rows}
            rows, seen = [], set()
            for row in left_rows:
                marker = key(row)
                if marker in right_keys and marker not in seen:
                    seen.add(marker)
                    rows.append(row)
        elif node.op == "EXCEPT":
            right_keys = {key(row) for row in right_rows}
            rows, seen = [], set()
            for row in left_rows:
                marker = key(row)
                if marker not in right_keys and marker not in seen:
                    seen.add(marker)
                    rows.append(row)
        else:
            raise ExecError(f"unsupported compound operator {node.op}")

        for expression, descending in reversed(node.order_by):
            index = None
            if isinstance(expression, ast.Literal) and isinstance(expression.value, int) \
                    and not isinstance(expression.value, bool):
                index = expression.value
                if index < 1 or index > len(left_items):
                    raise ExecError(f"{_ordinal(index)} ORDER BY term out of range - "
                                    f"should be between 1 and {len(left_items)}")
                index -= 1
            elif isinstance(expression, ast.ColumnRef):
                lowered = [name.lower() for name in left_items]
                if expression.name.lower() not in lowered:
                    raise ExecError(f"{_ordinal(order_position(node, expression))} ORDER BY term "
                                    "does not match any column in the result set")
                index = lowered.index(expression.name.lower())
            else:
                raise ExecError("1st ORDER BY term does not match any column in the result set")
            rows.sort(key=lambda row, i=index: _order_sort_key(row[i]), reverse=descending)

        rows = self._apply_limit(node, rows)
        return left_items, rows

    def _select_node(self, node):
        if isinstance(node, ast.Compound):
            return self._compound(node)
        return self._select(node)

    def _select_simple(self, node: ast.Select):
        table = self.table(node.table)
        # Invariant: after this loop `projection` holds only SelectItem nodes. `SELECT *` is
        # expanded here once, so no later pass has to branch on Star (the M12 crash was a pass
        # that assumed every entry was a SelectItem).
        projection = []
        items = []
        for item in node.items:
            if isinstance(item, ast.Star):
                for name in table.column_names:
                    projection.append(ast.SelectItem(expr=ast.ColumnRef(name=name)))
                    items.append(name)
            else:
                projection.append(item)
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

        prepared = []
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
            prepared.append((env, row))

        window_values = {}
        for position, item in enumerate(projection):
            if isinstance(item.expr, ast.WindowFunc):
                window_values[position] = self._window_values(item.expr, prepared, table)

        entries = []
        for index, (env, row) in enumerate(prepared):
            aliases = {}
            for position, item in enumerate(projection):
                if not item.alias:
                    continue
                if position in window_values:
                    aliases[item.alias.lower()] = window_values[position][index]
                else:
                    aliases[item.alias.lower()] = self._eval(item.expr, table, env)
            entries.append({"env": env, "row": row, "aliases": aliases,
                            "window": {position: window_values[position][index]
                                       for position in window_values}})

        for expression, descending in reversed(node.order_by):
            def key(entry, expression=expression):
                if isinstance(expression, ast.ColumnRef) and expression.name.lower() in entry["aliases"]:
                    return _order_sort_key(entry["aliases"][expression.name.lower()])
                return self._order_key(self._eval(expression, table, entry["env"]),
                                       self._collation(expression, table))
            entries.sort(key=key, reverse=descending)

        rows = []
        for entry in entries:
            projected = []
            for position, item in enumerate(projection):
                if position in entry["window"]:
                    projected.append(entry["window"][position])
                else:
                    projected.append(self._eval(item.expr, table, entry["env"]))
            rows.append(tuple(projected))

        # DISTINCT before LIMIT: LIMIT counts distinct rows, not source rows.
        if node.distinct:
            rows = _distinct(rows)
        rows = self._apply_limit(node, rows)

        self.last_plan = {"table": table.name, "access": access, "index": index_name,
                          "rows_examined": examined, "rows_matched": len(rows),
                          "reason": decision["reason"],
                          "estimated_rows": decision["estimated_rows"],
                          "candidates": decision["candidates"]}
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

            grouped = []
            for key in order:
                group = groups[key]
                if node.having is not None and not truthy(self._eval(node.having, None, group[0], group)):
                    continue
                grouped.append((group[0] if group else {}, group))

            for expression, descending in reversed(node.order_by):
                def key(entry, expression=expression):
                    env, group = entry
                    aliased = {}
                    for item in projection:
                        if isinstance(item, ast.SelectItem) and item.alias and not isinstance(item.expr, ast.Star):
                            aliased[item.alias.lower()] = self._eval(item.expr, None, env, group)
                    if isinstance(expression, ast.ColumnRef) and expression.name.lower() in aliased:
                        return _order_sort_key(aliased[expression.name.lower()])
                    return _order_sort_key(self._eval(expression, None, env, group))
                grouped.sort(key=key, reverse=descending)

            for env, group in self._apply_limit(node, grouped):
                rows.append(tuple(self._project(item, group) for item in projection))
        else:
            prepared = [(env, [env]) for env in envs]
            for expression, descending in reversed(node.order_by):
                def key(entry, expression=expression):
                    env, group = entry
                    return _order_sort_key(self._eval(expression, None, env, group))
                prepared.sort(key=key, reverse=descending)
            for env, group in self._apply_limit(node, prepared):
                rows.append(tuple(self._project(item, group) for item in projection))

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

    def _order_value(self, expr, env, aliases=None):
        if isinstance(expr, ast.ColumnRef) and aliases and expr.name.lower() in aliases:
            return aliases[expr.name.lower()]
        return self._eval(expr, None, env)

    def _apply_limit(self, node, items):
        limit = None
        offset = 0
        if node.limit is not None:
            limit = int(to_number(self._eval(node.limit, None, {})))
            if limit < 0:
                limit = None
        if node.offset is not None:
            offset = int(to_number(self._eval(node.offset, None, {})))
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

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
            _, rows = self._select_node(select)
        finally:
            self.last_plan = saved
        if not rows:
            return None
        return rows[0][0]

    def _in_subquery(self, node, env, group):
        value = self._eval(node.expr, None, env, group)
        saved = self.last_plan
        try:
            _, rows = self._select_node(node.select)
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
            # Aggregates must be resolved before their arguments are evaluated: over an empty
            # group there is no row to evaluate a column reference against.
            if node.name in self.AGGREGATES and len(node.args or []) <= 1:
                if group is None:
                    raise ExecError(f"misuse of aggregate function {node.name}()")
                return self._aggregate(node, group)
            arguments = [self._eval(argument, table, env, group) for argument in (node.args or [])]
            return call_function(node.name, arguments)
        if isinstance(node, ast.Raise):
            raise ExecError(node.message if node.message else node.action.lower())
        if isinstance(node, ast.ColumnRef):
            if node.table and self.trigger_env is not None:
                qualified = f"{node.table.lower()}.{node.name.lower()}"
                if qualified in self.trigger_env:
                    return self.trigger_env[qualified]
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
            nocase = (self._collation(node.left, table) == "NOCASE"
                      or self._collation(node.right, table) == "NOCASE")
            return sql_compare(self._eval(node.left, table, env, group),
                               self._eval(node.right, table, env, group), node.op, nocase)
        if isinstance(node, ast.Collate):
            return self._eval(node.expr, table, env, group)
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


def _render_expression(node):
    if isinstance(node, ast.Literal):
        if isinstance(node.value, str):
            return "'" + node.value.replace("'", "''") + "'"
        if node.value is None:
            return "NULL"
        return str(node.value)
    if isinstance(node, ast.ColumnRef):
        return node.name
    if isinstance(node, ast.UnaryOp):
        return f"{node.op} {_render_expression(node.operand)}"
    if isinstance(node, ast.BinaryOp):
        return f"({_render_expression(node.left)} {node.op} {_render_expression(node.right)})"
    return "NULL"


def _references(node, name):
    """True when a select reads from `name` anywhere (used to spot recursive CTE arms)."""
    if node is None:
        return False
    if isinstance(node, ast.Select):
        if node.table and node.table.lower() == name.lower():
            return True
        if any(join.table and join.table.lower() == name.lower() for join in node.joins):
            return True
        return _references(node.base_select, name)
    if isinstance(node, ast.Compound):
        return _references(node.left, name) or _references(node.right, name)
    return False


def _value_key(value):
    """Row identity for set operations: numbers compare by value, so 1 and 1.0 are one row."""
    if value is None:
        return ("null",)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("num", float(value))
    if isinstance(value, bytes):
        return ("blob", value)
    return ("text", value)


def _ordinal(number):
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def order_position(node, expression):
    for position, (candidate, _) in enumerate(node.order_by, start=1):
        if candidate is expression:
            return position
    return 1


def _order_sort_key(value):
    """SQLite ordering: NULLs first, then numbers, text, blobs."""
    if value is None:
        return (0, 0)
    return (_RANK.get(type(value), 9), value)


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
