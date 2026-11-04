"""In-memory storage and execution for the MiniDB SQL subset (milestone 2).

Values are Python objects: int, float, str, bytes, None. Type affinity (SQLite's dynamic
typing and column affinity rules) is milestone 17 and deliberately not attempted here — what
milestone 2 promises is that statements execute and that identity errors are reported with
SQLite's own wording.

Error text is part of the contract: SQLite's messages are what the conformance harness
compares, so they are produced here rather than reworded by a caller.
"""

import bisect
import copy
import os
from functools import lru_cache
import re
import string

from .sql import ast
# The C accelerator is optional: without it the Python path below runs, which is the reference
# every conformance case pins. A machine without a compiler loses speed, never answers.
try:
    from . import _native
except ImportError:
    _native = None
from .storage.disk import DiskStorage
from .storage.pager import PAGE_SIZE, StorageError
from .storage.sqlite_file import SQLiteFile, is_sqlite_file

# SQLite's storage-class ordering, used when comparing values of different types.
_RANK = {type(None): 0, int: 1, float: 1, str: 2, bytes: 3}


class ExecError(Exception):
    """Mirrors sqlite3.Error: message text should match SQLite's own where one exists."""


def _rank(value):
    return _RANK[type(value)]


_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def ascii_fold(text):
    """SQLite's NOCASE mapping: the 26 ASCII letters and nothing else.

    Python's str.lower() is Unicode-aware and would fold 'Ä' to 'ä', which SQLite does not do --
    skeptic round 1, case 16 caught exactly that over-folding.
    """
    return text.translate(_ASCII_FOLD)


def sql_compare(left, right, op, nocase=False):
    """Three-valued comparison: NULL in either side yields None, as in SQL."""
    if left is None or right is None:
        return None
    if nocase and isinstance(left, str) and isinstance(right, str):
        left, right = ascii_fold(left), ascii_fold(right)
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
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
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
    if type_name in ("BLOB",):
        if isinstance(value, bytes):
            return value
        return to_text(value).encode("utf-8", "surrogateescape")
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
    if value is None or isinstance(value, bytes) or affinity == "BLOB":
        return value             # blobs keep their storage class in every column
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


# -- JSON (milestone 35) ------------------------------------------------------
# SQLite's JSON layer is a text-in/text-out API with a small path language. Two behaviours are worth
# stating because they are easy to get subtly wrong: `json()` normalises whitespace but preserves key
# order, and `->` always yields text while `->>` yields the SQL value (numbers as numbers, strings
# unquoted, JSON null as SQL NULL).

_JSON_PATH_TOKEN = None


class JsonError(Exception):
    pass


def json_load(text):
    import json as _json
    try:
        return _json.loads(text)
    except Exception:
        raise JsonError("malformed JSON") from None


def json_dump(value):
    import json as _json
    return _json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def json_path_steps(path):
    """'$.a.b[2]' -> ['a', 'b', 2]. Raises on anything the reference calls a bad path."""
    if not isinstance(path, str) or not path.startswith("$"):
        raise JsonError(f"bad JSON path: '{path}'")
    steps, index, i = [], path, 1
    while i < len(index):
        if index[i] == ".":
            j = i + 1
            while j < len(index) and index[j] not in ".[":
                j += 1
            name = index[i + 1:j]
            if not name:
                raise JsonError(f"bad JSON path: '{path}'")
            steps.append(name)
            i = j
        elif index[i] == "[":
            j = index.find("]", i)
            if j < 0:
                raise JsonError(f"bad JSON path: '{path}'")
            body = index[i + 1:j]
            if body == "#":
                steps.append("#")
            elif body.lstrip("-").isdigit():
                steps.append(int(body))
            else:
                raise JsonError(f"bad JSON path: '{path}'")
            i = j + 1
        else:
            raise JsonError(f"bad JSON path: '{path}'")
    return steps


def json_lookup(value, steps):
    for step in steps:
        if step == "#":
            return len(value) if isinstance(value, list) else None
        if isinstance(step, int):
            if not isinstance(value, list) or step < 0 or step >= len(value):
                return None
            value = value[step]
        else:
            if not isinstance(value, dict) or step not in value:
                return None
            value = value[step]
    return value


def json_to_sql(value):
    """`->>` semantics: JSON value as an SQL value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list)):
        return json_dump(value)
    return value


def json_to_text(value):
    """`->` semantics: JSON text for any JSON value."""
    return json_dump(value)


def call_json_function(name, args):
    if name == "JSON":
        if args[0] is None:
            return None
        return json_dump(json_load(to_text(args[0])))
    if name == "JSON_VALID":
        if args[0] is None:
            return None
        try:
            json_load(to_text(args[0]))
        except JsonError:
            return 0
        return 1
    if name == "JSON_TYPE":
        value = json_load(to_text(args[0]))
        if len(args) > 1 and args[1] is not None:
            value = json_lookup(value, json_path_steps(to_text(args[1])))
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, dict):
            return "object"
        if isinstance(value, list):
            return "array"
        if isinstance(value, str):
            return "text"
        if isinstance(value, int):
            return "integer"
        return "real"
    if name == "JSON_EXTRACT":
        value = json_lookup(json_load(to_text(args[0])), json_path_steps(to_text(args[1])))
        return json_to_sql(value)
    if name == "JSON_ARRAY":
        return json_dump([_json_input(value) for value in args])
    if name == "JSON_OBJECT":
        pairs = list(zip(args[0::2], args[1::2]))
        return json_dump({to_text(key): _json_input(value) for key, value in pairs})
    if name == "JSON_QUOTE":
        value = args[0]
        if value is None:
            return "null"
        if isinstance(value, (int, float)):
            return json_dump(value)
        return json_dump(to_text(value))
    if name in ("JSON_SET", "JSON_INSERT", "JSON_REPLACE"):
        document = json_load(to_text(args[0]))
        mode = {"JSON_SET": "set", "JSON_INSERT": "insert", "JSON_REPLACE": "replace"}[name]
        for index in range(1, len(args) - 1, 2):
            json_mutate(document, to_text(args[index]), _json_input(args[index + 1]), mode)
        return json_dump(document)
    if name == "JSON_REMOVE":
        document = json_load(to_text(args[0]))
        for index in range(1, len(args)):
            json_remove_path(document, to_text(args[index]))
        return json_dump(document)
    if name == "JSON_ARRAY_LENGTH":
        value = json_load(to_text(args[0]))
        if len(args) > 1 and args[1] is not None:
            value = json_lookup(value, json_path_steps(to_text(args[1])))
        return len(value) if isinstance(value, list) else 0
    raise JsonError(f"no such function: {name}")


def json_mutate(document, path, value, mode):
    """json_set / json_insert / json_replace semantics, including the reference's refusal to
    extend an array past its end (json_set('[1]','$[3]',9) leaves '[1]' alone)."""
    steps = json_path_steps(path)
    if not steps:
        return value if mode != "remove" else document
    node = document
    for step in steps[:-1]:
        if isinstance(step, int):
            if not isinstance(node, list) or step >= len(node):
                return document
            node = node[step]
        else:
            if not isinstance(node, dict):
                return document
            if step not in node:
                if mode == "insert" or mode == "set":
                    node[step] = {}
                else:
                    return document
            node = node[step]
    last = steps[-1]
    if isinstance(last, int):
        if not isinstance(node, list) or last >= len(node):
            return document
        if mode != "insert":
            node[last] = value
        return document
    if not isinstance(node, dict):
        return document
    if mode == "insert" and last in node:
        return document
    if mode == "replace" and last not in node:
        return document
    node[last] = value
    return document


def json_remove_path(document, path):
    steps = json_path_steps(path)
    if not steps:
        return document
    node = document
    for step in steps[:-1]:
        if isinstance(step, int):
            if not isinstance(node, list) or step >= len(node):
                return document
            node = node[step]
        else:
            if not isinstance(node, dict) or step not in node:
                return document
            node = node[step]
    last = steps[-1]
    if isinstance(last, int):
        if isinstance(node, list) and last < len(node):
            del node[last]
    elif isinstance(node, dict):
        node.pop(last, None)
    return document


def _json_input(value):
    if isinstance(value, bytes):
        return text_of(value)
    return value


# -- full-text search (milestone 34) ------------------------------------------
# The declared subset: plain terms, AND / OR / NOT, column filters (title:dog), trailing prefix
# (qui*), unicode61-style case folding. bm25(), NEAR and tokenizer options are declared out of
# scope rather than silently unimplemented.

_WORD = None


def fts_tokens(text):
    import re
    global _WORD
    if _WORD is None:
        _WORD = re.compile(r"[0-9A-Za-z_\u00c0-\uffff]+")
    return [match.group(0).lower() for match in _WORD.finditer(text or "")]


def fts_parse_query(query):
    """Split an fts5 query into its declared subset's AST: ('or', ...), ('and', ...), ('not', ...),
    ('term', column_or_None, word, prefix)."""
    parts = query.replace("(", " ( ").replace(")", " ) ").split()

    def atom(tokens, index):
        word = tokens[index]
        column = None
        if ":" in word and not word.startswith(":"):
            column, word = word.split(":", 1)
        prefix = word.endswith("*")
        if prefix:
            word = word[:-1]
        return ("term", column, word.lower(), prefix), index + 1

    def factor(tokens, index):
        if index < len(tokens) and tokens[index].upper() == "NOT":
            node, index = factor(tokens, index + 1)
            return ("not", node), index
        if index < len(tokens) and tokens[index] == "(":
            node, index = expression(tokens, index + 1)
            if index < len(tokens) and tokens[index] == ")":
                index += 1
            return node, index
        return atom(tokens, index)

    def conjunction(tokens, index):
        node, index = factor(tokens, index)
        while index < len(tokens) and tokens[index].upper() not in ("OR", ")"):
            if tokens[index].upper() == "AND":
                index += 1
            right, index = factor(tokens, index)
            node = ("and", node, right)
        return node, index

    def expression(tokens, index):
        node, index = conjunction(tokens, index)
        while index < len(tokens) and tokens[index].upper() == "OR":
            right, index = conjunction(tokens, index + 1)
            node = ("or", node, right)
        return node, index

    return expression(parts, 0)[0]


def fts_eval(node, columns):
    kind = node[0]
    if kind == "term":
        _, column, word, prefix = node
        scope = columns if column is None else {column.lower(): columns.get(column.lower(), [])}
        for tokens in scope.values():
            for token in tokens:
                if token.startswith(word) if prefix else token == word:
                    return True
        return False
    if kind == "and":
        return fts_eval(node[1], columns) and fts_eval(node[2], columns)
    if kind == "or":
        return fts_eval(node[1], columns) or fts_eval(node[2], columns)
    return not fts_eval(node[1], columns)


def call_function(name, args):
    """Scalar built-ins (milestone 11). NULL handling follows SQLite, which is not uniform."""
    if name.startswith("JSON"):
        return call_json_function(name, args)
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
    if name == "QUOTE":
        value = args[0]
        if value is None:
            return "NULL"
        if isinstance(value, bytes):
            return "X'" + value.hex().upper() + "'"
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return to_text(value)
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
    if name == "ZEROBLOB":
        size = to_number(args[0])
        if size is None or size < 0:
            raise ExecError("string or blob too big")
        return bytes(int(size))
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

    if name == "HEX":
        value = args[0]
        if isinstance(value, bytes):
            return value.hex().upper()
        return text_of(value).encode("utf-8").hex().upper()
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
        # SQLite's 'now' is UTC; using local time makes date('now') disagree for eight hours a day.
        return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
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
                 "check_text", "ref_table", "ref_column", "affinity", "collation",
                 "auto_increment")

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
        self.auto_increment = False
        for constraint in definition.constraints:
            if constraint.kind == "PRIMARY KEY" and constraint.text == "AUTOINCREMENT":
                self.auto_increment = True
            if constraint.kind == "COLLATE":
                self.collation = (constraint.text or "").upper()
            elif constraint.kind == "REFERENCES":
                self.ref_table = constraint.ref_table
                self.ref_column = constraint.ref_column
            elif constraint.kind == "PRIMARY KEY":
                # SQLite does NOT enforce NOT NULL on a PRIMARY KEY in a rowid table (the historical
                # bug the docs still document): a TEXT PRIMARY KEY stores NULL, and an INTEGER
                # PRIMARY KEY turns NULL into an assigned key. Enforcing it here made MiniDB stricter
                # than the reference -- a divergence the differential cannot excuse. (WITHOUT ROWID
                # tables, where the standard rule does apply, are out of scope.)
                self.primary_key = True
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


def _integer_rowid(column):
    """A single-column PRIMARY KEY over INTEGER is the rowid in both engines."""
    return column.type_name.strip().upper() == "INTEGER"


def _is_rowid_alias(column):
    """SQLite aliases the rowid only for a PRIMARY KEY whose declared type is exactly INTEGER.

    `INT PRIMARY KEY`, `TEXT PRIMARY KEY` and a composite PRIMARY KEY all get an autoindex; a
    table-level `PRIMARY KEY (a)` on an INTEGER column aliases exactly like the column-level form.
    """
    return column.primary_key and column.type_name.strip().upper() == "INTEGER"


ROWID_SPELLINGS = ("rowid", "_rowid_", "oid")


@lru_cache(maxsize=1024)
def _parse_cached(sql):
    """Parsing is pure and the AST is read-only during execution, so the same statement text can
    reuse one tree. The benchmark's point-lookup workload paid the parser on every call."""
    from .sql.parser import parse
    return parse(sql)


RANDOM_ACCESS_COST = 20      # measured: ~250us random row fetch vs ~10us sequential row read
PLAN_CACHE_MAX = 128         # remembered plans per session; cleared wholesale when full or on a write
PLAN_CACHE_ROWS = 4096       # don't remember a decision that materialised a huge rowid set

NATIVE_AGGREGATES = {"COUNT": 1, "SUM": 2, "AVG": 3, "MIN": 4, "MAX": 5}


class Index:
    """Hash index over one or more columns (milestone 4).

    Keys are the column values in index order. NULLs never conflict under SQLite's uniqueness
    rules, so rows containing a NULL key are indexed but exempt from the unique check.
    """

    __slots__ = ("name", "table", "columns", "column_names", "unique", "map", "where")

    def __init__(self, name, table, columns, column_names, unique):
        self.name = name
        self.table = table
        self.columns = columns              # positional indices into the row
        self.column_names = column_names
        self.unique = unique
        self.map = {}                       # key tuple -> set of rowids
        self.where = None                   # partial-index predicate (None = every row)

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

    def lookup_range(self, op, value):
        """Rows whose first indexed column satisfies `col op value`.

        The hash map is keyed by value, so a range means probing every distinct key. That is
        O(distinct keys), not O(rows), which is why it still beats a full scan on a selective range.
        """
        rowids = set()
        for key, bucket in self.map.items():
            if len(key) != 1:
                continue
            if sql_compare(key[0], value, op):
                rowids |= bucket
        return rowids

    def add(self, rowid, values):
        self.check_unique(values)
        self.map.setdefault(self.key_for(values), set()).add(rowid)

    def remove(self, rowid, values):
        key = self.key_for(values)
        bucket = self.map.get(key)
        if bucket:
            bucket.discard(rowid)
            if not bucket:
                self.map.pop(key, None)

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
        # Column-level UNIQUE/PRIMARY KEY constraints: position -> set of values seen. Built on
        # first insert and maintained per insert; cleared by any other row change (M38's
        # write-path item: the old check scanned the table per row, which is O(n^2) to load).
        self.unique_sets = {}
        self.next_rowid = 1                     # in-memory rowids; disk keeps its own counter
        # In-memory rowids, parallel to `rows`. None means "positional" -- 1..n, which is what the
        # synthetic tables (views, CTEs, sqlite_master) want. Real tables keep the list so a DELETE
        # leaves a hole and an explicit rowid survives, exactly as the B+tree's keys do on disk.
        self.rowids = None

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

    def __init__(self, path=None, readonly=False, repl_log=False):
        # SQLite spells its private in-memory database ':memory:' (executed against the pinned CLI:
        # `sqlite3 ':memory:'` creates no file). Treating it as a filename diverged silently and
        # only surfaced as a lock message, so the spelling is honoured here.
        if path == ":memory:":
            path = None
        self.path = path
        if path and not readonly:
            # A VACUUM killed before its rename leaves `<db>.vacuum` and its WAL behind. Opening
            # the database is the only moment that knows the staging name is stale, so the sweep
            # lives here (skeptic round 2: a killed vacuum's orphans had never been tested).
            self._clear_vacuum_artifacts(path + ".vacuum")
        foreign = bool(path) and is_sqlite_file(path)
        try:
            self.storage = None if foreign else (
                DiskStorage(path, readonly=readonly, repl_log=repl_log) if path else None)
        except StorageError as exc:
            if str(exc) == "database is locked":
                raise ExecError(str(exc)) from None
            raise
        self.tables = {}
        self.views = {}
        self.triggers = {}
        self.trigger_env = None
        # Enclosing rows, innermost last, for correlated subqueries. A chain rather than a single
        # env: SQLite resolves a correlated name at any depth, so a subquery nested two levels deep
        # must still see the outermost query's row. Keeping only the immediate parent loses it —
        # the growth run's first finding ("no such column: a.k1" on a double-nested IN).
        self._outer_env = ()
        self._in_trigger = False
        self.foreign_keys = False
        self.sequences = {}
        self._sql = None
        self.last_plan = None
        # Plans this session has already chosen. Any non-read statement clears them: a decision
        # carries the rowid set it was made from, so a stale entry is a wrong answer, not a stale
        # statistic. (M37: the ladder's "learns from query history" — the history that is safe
        # here is the session's own; see docs/tier6-prework.md's M37 decision.)
        self._plan_cache = {}
        self.data_epoch = 0
        self.tx_snapshot = None
        self.repl_log = bool(repl_log)
        self.read_only = bool(readonly)
        if foreign:
            # A file the reference wrote is read-only by the M23 scope decision: MiniDB answers
            # queries over SQLite's bytes and never writes them back.
            self.read_only = True
            self._load_sqlite_file(path)
        elif self.storage is not None:
            try:
                self._load_schema()
            except Exception:
                # Opening is not complete until the schema is rebuilt, and that walk reads table
                # pages: truncation, a checksum mismatch or corrupt catalog SQL all land here.
                # The caller never receives this Database, so this is the last chance to release
                # the page file -- as a discard, since the open is being rejected.
                self.storage.abandon()
                raise

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
            self._derive_constraints(table, statement)

    def _load_sqlite_file(self, path):
        from .sql.parser import parse
        source = SQLiteFile(path)
        for obj in source.schema_objects():
            sql = obj.get("sql")
            if not sql:
                continue
            statement = parse(sql)
            if isinstance(statement, ast.CreateTable):
                columns = [Column(c) for c in statement.columns]
                table = Table(statement.name, columns)
                for _, values in source.table_rows(obj["root"]):
                    table.rows.append(tuple(values))
                table.next_rowid = len(table.rows) + 1
                self.tables[statement.name.lower()] = table
                self._derive_constraints(table, statement)
        for obj in source.schema_objects():
            sql = obj.get("sql")
            if not sql:
                continue
            statement = parse(sql)
            if isinstance(statement, ast.CreateIndex):
                self._create_index(statement)
            elif isinstance(statement, ast.CreateView):
                self.views[statement.name.lower()] = statement

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
        if not isinstance(statement, (ast.Select, ast.Compound, ast.With, ast.Pragma,
                                      ast.Analyze, ast.Vacuum)):
            self.data_epoch += 1
            self._plan_cache.clear()
        if (autocommit and self.storage is not None and self.tx_snapshot is None
                and not isinstance(statement, ast.Begin)):
            self.storage.commit()
        return result

    def _execute(self, statement):
        if self.read_only and not isinstance(statement, (ast.Select, ast.Compound, ast.With,
                                                         ast.Pragma, ast.Analyze, ast.Vacuum)):
            raise ExecError("attempt to write a readonly database")
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
            # Validation belongs to the statement's entry point, not to _select: a subquery reached
            # from _select during execution has no idea which scopes surround it, and validating it
            # there would reject every correlated reference (the depth-2 tests caught exactly that).
            self._validate_select(statement)
            return self._select(statement)
        if isinstance(statement, ast.Compound):
            return self._compound(statement)
        if isinstance(statement, ast.Vacuum):
            return self._vacuum()
        if isinstance(statement, ast.Explain):
            return self._explain(statement)
        if isinstance(statement, ast.Analyze):
            return None          # accepted; results unchanged (sqlite_stat1 contents are out of scope)
        if isinstance(statement, ast.DropTable):
            return self._drop_table(statement)
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
        return self.execute(_parse_cached(sql), autocommit=True)

    # -- CREATE --------------------------------------------------------------
    def _create(self, node: ast.CreateTable):
        key = node.name.lower()
        module = getattr(node, "virtual_module", None)
        if module is not None and module.lower() != "fts5":
            raise ExecError(f"no such module: {module}")
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
        # Column-level constraints are read from the definition, not from the derived flags: a
        # table-level PRIMARY KEY sets `unique` on its column too, and feeding that into this loop
        # would give the rowid alias the index the reference does not show.
        column_level = [position for position, definition in enumerate(node.columns)
                        if any(c.kind in ("UNIQUE", "PRIMARY KEY") for c in definition.constraints)]
        self._derive_constraints(table, node)
        # The reference shows an autoindex exactly where a constraint is not the rowid: every
        # column-level UNIQUE, every column-level PRIMARY KEY except the `INTEGER` rowid alias,
        # and every table-level constraint except a single-column `INTEGER` PRIMARY KEY. Numbering
        # follows the schema text — column constraints in column order, then table constraints —
        # because the names are visible in sqlite_master and a case can pin them.
        number = 1
        for position in column_level:
            column = table.columns[position]
            if column.primary_key and _is_rowid_alias(column):
                continue
            self._add_autoindex(table, number, [position], [column.name])
            number += 1
        for constraint in getattr(node, "table_constraints", []):
            if constraint.kind in ("PRIMARY KEY", "UNIQUE"):
                positions = [table.index_of[name.lower()] for name in constraint.columns]
                if (constraint.kind == "PRIMARY KEY" and len(positions) == 1
                        and _integer_rowid(table.columns[positions[0]])):
                    pass                  # see _derive_constraints: enforced without an index
                else:
                    self._add_autoindex(table, len(table.indexes) + 1, positions,
                                        list(constraint.columns))
            elif constraint.kind == "CHECK":
                table.checks = getattr(table, "checks", [])
                table.checks.append((constraint.expr, constraint.text))
        return None

    def _derive_constraints(self, table, statement):
        """The state a schema implies but its column list does not carry.

        A table-level `PRIMARY KEY (x)` over INTEGER is the rowid: no autoindex is created, so the
        constraint needs the uniqueness set — and it must be re-derived on load, because the
        catalog stores the SQL, not the derived flag. Without this a reopened file accepts the
        duplicate the fresh one refused.
        """
        for constraint in getattr(statement, "table_constraints", []):
            if constraint.kind != "PRIMARY KEY" or len(constraint.columns) != 1:
                continue
            position = table.index_of.get(constraint.columns[0].lower())
            if position is not None and _integer_rowid(table.columns[position]):
                table.columns[position].unique = True

    def _add_autoindex(self, table, number, positions, names):
        name = f"sqlite_autoindex_{table.name}_{number}"
        index = Index(name, table.name, list(positions), list(names), True)
        table.indexes[name.lower()] = index
        if self.storage is not None:
            self._persist_indexes(table)

    def _materialise_table_function(self, node):
        if node.name.lower() != "json_each":
            raise ExecError(f"no such table: {node.name}")
        document = json_load(to_text(self._eval(node.args[0], None, {})))
        columns = [Column(ast.ColumnDef(name=name, type_name="", constraints=[]))
                   for name in ("key", "value", "type", "atom", "id", "parent", "fullkey", "path")]
        table = Table(node.name, columns)
        items = document.items() if isinstance(document, dict) else enumerate(document)
        for index, (key, value) in enumerate(items):
            kind = ("null" if value is None else
                    "true" if value is True else "false" if value is False else
                    "object" if isinstance(value, dict) else
                    "array" if isinstance(value, list) else
                    "text" if isinstance(value, str) else
                    "integer" if isinstance(value, int) else "real")
            atom = value if not isinstance(value, (dict, list)) else None
            path = f"$[{key}]" if isinstance(document, list) else f'$.{key}'
            table.rows.append((key, json_to_sql(value) if not isinstance(value, (dict, list)) else json_dump(value),
                               kind, atom, index, None, path, "$"))
        return table

    def table(self, name):
        if isinstance(name, ast.TableFunction):
            return self._materialise_table_function(name)
        key = name.lower()
        if key == "sqlite_master":
            return self._master_table()
        if key == "sqlite_sequence":
            return self._sequence_table()
        if key in self.tables:
            return self.tables[key]
        if key in self.views:
            return self._materialise_view(self.views[key])
        raise ExecError(f"no such table: {name}")

    def _sequence_table(self):
        table = Table("sqlite_sequence", [Column(ast.ColumnDef(name=name))
                                          for name in ("name", "seq")])
        table.rows = [(name, seq) for name, seq in self.sequences.items()]
        return table

    def _master_table(self):
        """sqlite_master as a readable table: SQLite exposes its catalog this way, and M29's
        .schema will read it, so the shape has to match rather than be invented later."""
        table = Table("sqlite_master", [Column(ast.ColumnDef(name=name))
                                        for name in ("type", "name", "tbl_name", "rootpage", "sql")])
        rows = []
        for name, obj in self.tables.items():
            sql = obj.entry["sql"] if obj.entry else self._render_create(obj, obj.name)
            rows.append(("table", obj.name, obj.name, obj.entry["root"] if obj.entry else 0, sql))
            for index in obj.indexes.values():
                columns = ", ".join(obj.columns[i].name for i in index.columns)
                unique = "UNIQUE " if index.unique else ""
                rows.append(("index", index.name, obj.name, 0,
                             f"CREATE {unique}INDEX {index.name} ON {obj.name} ({columns})"))
        for name, view in self.views.items():
            rows.append(("view", view.name, view.name, 0,
                         view.sql or f"CREATE VIEW {view.name} AS SELECT 1"))
        for name, trigger in self.triggers.items():
            rows.append(("trigger", trigger.name, trigger.table, 0,
                         trigger.sql or f"CREATE TRIGGER {trigger.name}"))
        table.rows = rows
        return table

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

    def _ast_columns(self, node, acc=None):
        """Every column name an expression touches, for covering-index decisions."""
        if acc is None:
            acc = set()
        if isinstance(node, ast.ColumnRef):
            acc.add(node.name.lower())
        elif isinstance(node, ast.BinaryOp):
            self._ast_columns(node.left, acc)
            self._ast_columns(node.right, acc)
        elif isinstance(node, ast.UnaryOp):
            self._ast_columns(node.operand, acc)
        elif isinstance(node, ast.Collate):
            self._ast_columns(node.expr, acc)
        elif isinstance(node, ast.Between):
            self._ast_columns(node.expr, acc)
            self._ast_columns(node.low, acc)
            self._ast_columns(node.high, acc)
        elif isinstance(node, ast.IsNull):
            self._ast_columns(node.expr, acc)
        elif isinstance(node, ast.Case):
            for child in [node.base, node.else_result] + [w[0] for w in getattr(node, "whens", [])] \
                    + [w[1] for w in getattr(node, "whens", [])]:
                if child is not None:
                    self._ast_columns(child, acc)
        elif isinstance(node, ast.FuncCall):
            for argument in (node.args or []):
                self._ast_columns(argument, acc)
        elif isinstance(node, getattr(ast, "InList", ())) or isinstance(node, getattr(ast, "In", ())):
            self._ast_columns(node.expr, acc)
        elif isinstance(node, getattr(ast, "InSubquery", ())):
            self._ast_columns(node.expr, acc)
        return acc

    def _covering_index(self, node, table, index):
        """True when every column the query touches lives in the index.

        Then the rows come out of the index keys themselves and nothing random-accesses the table —
        the cost the M21 measurement identified as the dominant one.
        """
        if index.where is not None:
            return False          # keep the partial path simple until it is measured
        names = {name.lower() for name in index.column_names}
        needed = set()
        for item in node.items:
            if isinstance(item, ast.Star):
                needed |= {column.name.lower() for column in table.columns}
            else:
                needed |= self._ast_columns(item.expr)
        if node.where is not None:
            needed |= self._ast_columns(node.where)
        for expression, _ in node.order_by:
            needed |= self._ast_columns(expression)
        return needed <= names

    def _index_applies(self, index, table, values):
        """Partial indexes only contain rows that satisfy their predicate."""
        if index.where is None:
            return True
        env = {}
        for column, value in zip(table.columns, values):
            env[column.name.lower()] = value
            env[f"{table.name.lower()}.{column.name.lower()}"] = value
        return truthy(self._eval(index.where, table, env))

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
        index.where = node.where
        # Build against existing rows first: a UNIQUE index over duplicate data must fail, and a
        # failed CREATE must leave no half-built index behind.
        for rowid, values in self._rows_with_ids(table):
            if self._index_applies(index, table, values):
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
             "unique": index.unique,
             "where": _render_expression(index.where) if index.where is not None else None}
            for index in table.indexes.values()
        ]
        self.storage.save_catalog_entry(table.entry)

    def _rebuild_indexes(self, table):
        for definition in table.entry.get("indexes", []):
            names = definition["columns"]
            positions = [table.index_of[c.lower()] for c in names]
            index = Index(definition["name"], table.name, positions, names, definition["unique"])
            if definition.get("where"):
                from .sql.parser import parse
                predicate = parse(f"SELECT 1 WHERE {definition['where']}")
                index.where = predicate.where
            for rowid, values in self._rows_with_ids(table):
                index.add(rowid, values)
            table.indexes[definition["name"].lower()] = index

    def _rows_with_ids(self, table):
        """(rowid, row) pairs, lazily: an UPDATE that already knows its rowids must not
        materialise a hundred thousand pairs to reach one of them."""
        if table.on_disk:
            return self.storage.iter_rows_with_ids(table.entry)
        if table.rowids is None:
            return enumerate(table.rows, start=1)
        return zip(table.rowids, table.rows)

    def _rowid_alias_position(self, table):
        """Position of the INTEGER PRIMARY KEY that *is* the rowid, or None.

        Only a single-column PRIMARY KEY whose declared type is exactly INTEGER aliases; the
        autoindex rule in _derive_constraints uses the same predicate, so the two surfaces cannot
        disagree about which tables have an implicit key.
        """
        primary = [i for i, column in enumerate(table.columns) if column.primary_key]
        if len(primary) == 1 and _is_rowid_alias(table.columns[primary[0]]):
            return primary[0]
        return None

    def _ensure_rowids(self, table):
        """Materialise positional rowids before the first mutation that could leave a hole."""
        if table.rowids is None:
            table.rowids = list(range(1, len(table.rows) + 1))
        return table.rowids

    def _next_memory_rowid(self, table):
        return max([table.next_rowid] + (table.rowids or [])) if table.rowids else table.next_rowid

    # -- INSERT --------------------------------------------------------------
    def _insert(self, node: ast.Insert):
        if node.table.lower() in self.views and node.table.lower() not in self.tables:
            # SQLite views are read-only unless they have INSTEAD OF triggers (not in scope yet).
            raise ExecError(f"cannot modify {node.table} because it is a view")
        table = self.table(node.table)
        alias_position = self._rowid_alias_position(table)
        if node.columns is None:
            targets = list(range(len(table.columns)))
        else:
            targets = []
            for name in node.columns:
                index = table.index_of.get(name.lower())
                if index is None:
                    # `INSERT INTO t (rowid, a)` writes the rowid itself; the three spellings are
                    # the reference's, and a real column of the same name wins (t6d's case L).
                    if name.lower() in ("rowid", "_rowid_", "oid"):
                        targets.append(None)
                        continue
                    raise ExecError(f"table {table.name} has no column named {name}")
                targets.append(index)

        for row in node.rows:
            pending_unique = []
            if len(row) != len(targets):
                raise ExecError(
                    f"table {table.name} has {len(targets)} columns but {len(row)} values were supplied"
                )
            values: list = [None] * len(table.columns)
            supplied = {target for target in targets if target is not None}
            forced_rowid = None
            for index, expr in zip(targets, row):
                if index is None:
                    forced_rowid = self._eval(expr, table, None)
                    continue
                values[index] = self._eval(expr, table, None)
            if alias_position is not None:
                # An INTEGER PRIMARY KEY *is* the rowid: an explicit value sets the key, and an
                # omitted one is assigned here -- before the NOT NULL/default checks below, because
                # the reference fills the key rather than reporting it as NULL. (Skeptic round 1,
                # case 27: the check ran first and turned a legal insert into a constraint error.)
                if values[alias_position] is not None:
                    forced_rowid = values[alias_position]
                else:
                    forced_rowid = self._next_memory_rowid(table) if not table.on_disk \
                        else max(table.entry["next_rowid"], 1)
                    values[alias_position] = forced_rowid
                # The key counts as supplied: the loop below fills defaults for omitted columns and
                # would otherwise report the key we just assigned as a missing NOT NULL value.
                supplied.add(alias_position)
            for i, column in enumerate(table.columns):
                if i in supplied:
                    continue
                if column.auto_increment:
                    current = self.sequences.get(table.name.lower(), 0)
                    highest = max([row[i] for row in self._rows(table)
                                   if isinstance(row[i], int)], default=0)
                    values[i] = max(current, highest) + 1
                    continue
                if column.default is not None:
                    values[i] = self._eval(column.default, table, None)
                elif column.not_null:
                    raise ExecError(f"NOT NULL constraint failed: {table.name}.{column.name}")
            for i, column in enumerate(table.columns):
                if column.auto_increment and values[i] is None:
                    current = self.sequences.get(table.name.lower(), 0)
                    highest = max([row[i] for row in self._rows(table)
                                   if isinstance(row[i], int)], default=0)
                    values[i] = max(current, highest) + 1
                values[i] = apply_affinity(values[i], column.affinity)
                if values[i] is None and column.not_null:
                    raise ExecError(f"NOT NULL constraint failed: {table.name}.{column.name}")
                if values[i] is None:
                    continue
                if column.check is not None and not truthy(self._eval(column.check, table, dict(zip(table.column_names, values)))):
                    raise ExecError(f"CHECK constraint failed: {column.check_text}")
                if column.unique:
                    bucket = self._unique_bucket(table, i, column)
                    key = self._unique_key(column, values[i])
                    if key in bucket:
                        raise ExecError(f"UNIQUE constraint failed: {table.name}.{column.name}")
                    # Added only once the row has actually landed: an insert that fails a later
                    # check (foreign key, unique index, trigger) must not reserve its key.
                    pending_unique.append((bucket, key))
            for check_expr, check_text in getattr(table, "checks", []):
                if not truthy(self._eval(check_expr, table, dict(zip(table.column_names, values)))):
                    raise ExecError(f"CHECK constraint failed: {check_text}")
            # Unique indexes are checked before the row lands, so a rejected insert leaves
            # neither a row nor a dangling index entry.
            for index in table.indexes.values():
                if self._index_applies(index, table, values):
                    index.check_unique(values)
            violation = self._foreign_key_violation(table, values)
            if violation:
                raise ExecError(violation)
            self._fire(table, "INSERT", "BEFORE", None, values)
            if forced_rowid is not None and self._row_by_id(table, forced_rowid) is not None:
                name = table.name if alias_position is None else table.columns[alias_position].name
                who = "rowid" if alias_position is None else name
                raise ExecError(f"UNIQUE constraint failed: {table.name}.{who}")
            if table.on_disk:
                try:
                    rowid = self.storage.insert_row(table.entry, values, rowid=forced_rowid)
                except StorageError as exc:
                    raise ExecError(str(exc)) from None
            else:
                rowid = forced_rowid if forced_rowid is not None else table.next_rowid
                self._ensure_rowids(table)      # before the append: it numbers the existing rows
                table.rows.append(tuple(values))
                table.rowids.append(rowid)
                table.next_rowid = max(table.next_rowid, rowid) + 1
            for bucket, key in pending_unique:
                bucket.add(key)
            for index in table.indexes.values():
                if self._index_applies(index, table, values):
                    index.add(rowid, values)
            for position, column in enumerate(table.columns):
                if column.auto_increment and isinstance(values[position], int):
                    self.sequences[table.name.lower()] = max(
                        self.sequences.get(table.name.lower(), 0), values[position])
                    if table.entry is not None:
                        table.entry["seq"] = self.sequences[table.name.lower()]
                        self.storage.save_catalog_entry(table.entry)
            self._fire(table, "INSERT", "AFTER", None, values)
        return len(node.rows)

    # -- DROP TABLE ----------------------------------------------------------
    def _drop_table(self, node: ast.DropTable):
        key = node.name.lower()
        if key in self.tables:
            del self.tables[key]
            if self.storage is not None:
                self.storage.drop_entry(node.name)
            return None
        if node.if_exists:
            return None
        raise ExecError(f"no such table: {node.name}")

    # -- EXPLAIN -------------------------------------------------------------
    def _explain(self, node: ast.Explain):
        """Human-readable plan. Deliberately NOT SQLite's bytecode or its planner vocabulary:
        those describe SQLite's VM and SQLite's choices, and diffing them would be meaningless.
        The shape mirrors `EXPLAIN QUERY PLAN`'s four columns so tools can read it."""
        statement = node.statement
        if not isinstance(statement, (ast.Select, ast.Compound)):
            return (["detail"], [("MiniDB: only SELECT plans are explained",)])
        plan = self._plan_for(statement)
        rows = [(0, 0, 0, detail) for detail in plan]
        return (["id", "parent", "notused", "detail"], rows)

    def _plan_for(self, statement):
        if isinstance(statement, ast.Compound):
            details = [f"COMPOUND {statement.op}"]
            details.extend(self._plan_for(statement.left))
            details.extend(self._plan_for(statement.right))
            return details
        if self._is_complex(statement):
            sources = self._sources(statement)
            details = ["NESTED LOOP" if len(sources) > 1 else "SCAN"]
            for source in sources:
                details.append(f"  TABLE {source['alias'] or source['name']}")
            if statement.group_by:
                details.append("  GROUP BY")
            return details
        decision = self._choose_access(self.table(statement.table), statement.where,
                                       node=statement)
        if decision["access"] == "index":
            index = decision["candidate"]["index"]
            return [f"SEARCH {statement.table} USING INDEX {index.name}",
                    f"  estimated rows: {decision['estimated_rows']}"]
        return [f"SCAN {statement.table}", f"  reason: {decision['reason']}"]

    # -- VACUUM --------------------------------------------------------------
    @staticmethod
    def _clear_vacuum_artifacts(base):
        """Remove VACUUM's staged files: the temp database and any WAL it wrote beside it."""
        for candidate in (base, base + ".wal", base + "-wal", base + ".shm", base + "-shm"):
            try:
                os.remove(candidate)
            except OSError:
                pass

    def _vacuum(self):
        """Rebuild the file so pages freed by DELETE/UPDATE are actually returned.

        There is no freelist yet, so compaction is the only way to reclaim them; rewriting into a
        fresh file also drops orphaned overflow chains left by row rewrites.
        """
        if self.storage is None:
            # VACUUM rebuilds the storage, and a rebuild renumbers the rowids: 1..n in row order.
            # In memory there is no file to rewrite, but the renumbering is part of the contract
            # (t6d's case K), so it happens here too.
            for table in self.tables.values():
                alias = self._rowid_alias_position(table)
                if alias is not None:
                    # An INTEGER PRIMARY KEY is data, not position: SQLite's VACUUM preserves those
                    # keys (it only renumbers tables whose rowid is not visible). Renumbering them
                    # here made the alias column disagree with its own rowid.
                    table.rowids = [row[alias] for row in table.rows]
                    table.next_rowid = max(table.rowids, default=0) + 1
                elif table.rowids is not None:
                    table.rowids = list(range(1, len(table.rows) + 1))
                    table.next_rowid = len(table.rows) + 1
                # Renumbering rewrites the alias column's values, so any key set built from the old
                # numbering is stale (skeptic round 1, case 27: it reported a UNIQUE conflict against
                # a key that VACUUM itself had just freed).
                table.unique_sets.clear()
            return None
        temp_path = self.path + ".vacuum"
        self._clear_vacuum_artifacts(temp_path)
        fresh = Database(temp_path)
        for table in self.tables.values():
            fresh.run(table.entry["sql"])
        for view in self.views.values():
            fresh.run(view.sql or f"CREATE VIEW {view.name} AS SELECT 1")
        for trigger in self.triggers.values():
            fresh.run(trigger.sql or f"CREATE TRIGGER {trigger.name} AFTER INSERT ON "
                                    f"{trigger.table} BEGIN SELECT 1; END")
        for table in self.tables.values():
            target = fresh.tables[table.name.lower()]
            for row in self._rows(table):
                if target.on_disk:
                    fresh.storage.insert_row(target.entry, list(row))
                else:
                    target.rows.append(tuple(row))
            for index in table.indexes.values():
                columns = ", ".join(table.columns[i].name for i in index.columns)
                unique = "UNIQUE " if index.unique else ""
                fresh.run(f"CREATE {unique}INDEX mdb_vac_{index.name} ON {table.name} ({columns})")
        fresh.close()
        self.storage.close()
        # VACUUM's file-replace path is its own crash surface: the temporary file can be half
        # written, or the process can die either side of the atomic rename.
        if os.environ.get("MINIDB_CRASH_POINT") == "vacuum_before_replace":
            os._exit(9)
        if os.environ.get("MINIDB_CRASH_POINT") == "vacuum_mid_replace":
            with open(temp_path, "r+b") as handle:   # half a file, as a torn write would leave it
                handle.truncate(max(0, os.path.getsize(temp_path) // 2))
            os._exit(9)
        os.replace(temp_path, self.path)
        # The staging handle wrote its own WAL next to the temp file; the rename moved the main
        # file only, so the sidecar would survive as `<db>.vacuum.wal` (skeptic round 2). It is a
        # stale WAL with no database beside it, which is the foreign-data-on-open hazard.
        self._clear_vacuum_artifacts(temp_path)
        if os.environ.get("MINIDB_CRASH_POINT") == "vacuum_after_replace":
            os._exit(9)
        reopened = Database(self.path, repl_log=self.repl_log)
        # A file rebuild is one full-image batch: page-level shipping cannot express a page that
        # vanished, so the replica gets the new file's pages wholesale.
        self.storage = reopened.storage
        if self.repl_log:
            self.storage.pager.checkpoint()
        self.tables = reopened.tables
        self.views = reopened.views
        self.triggers = reopened.triggers
        reopened.storage = None      # ownership moved to self; avoid double close
        return None

    # -- PRAGMA / foreign keys ----------------------------------------------
    def _pragma(self, node: ast.Pragma):
        name = node.name.lower()
        if name == "integrity_check":
            return (["integrity_check"], [(self._integrity_check(),)])
        if name == "table_info":
            if node.value is None:
                raise ExecError("table_info requires a table name")
            if isinstance(node.value, ast.ColumnRef):
                target_name = node.value.name
            elif isinstance(node.value, ast.Literal):
                target_name = str(node.value.value)
            else:
                raise ExecError("table_info requires a table name")
            target = self.table(target_name)
            rows = []
            for position, column in enumerate(target.columns):
                default = _render_expression(column.default) if column.default is not None else None
                rows.append((position, column.name, column.type_name or "", 1 if column.not_null else 0,
                             default, 1 if column.primary_key else 0))
            return (["cid", "name", "type", "notnull", "dflt_value", "pk"], rows)
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

    def _integrity_check(self):
        """SQLite's PRAGMA integrity_check answers 'ok' or names the first problem it finds."""
        try:
            if self.storage is None:
                for table in self.tables.values():
                    list(self._rows(table))
                return "ok"
            size = os.path.getsize(self.path)
            if size % PAGE_SIZE:
                return f"file size {size} is not a multiple of the page size"
            pager = self.storage.pager
            if size // PAGE_SIZE != pager.page_count:
                return f"header says {pager.page_count} pages, file has {size // PAGE_SIZE}"
            for entry in self.storage.catalog.values():
                if entry.get("kind") == "view":
                    continue
                for _ in self.storage.iter_rows_with_ids(entry):
                    pass
            for table in self.tables.values():
                rows = list(self._rows(table))
                for index in table.indexes.values():
                    indexed = sum(len(bucket) for bucket in index.map.values())
                    if indexed != len(rows):
                        return f"index {index.name} has {indexed} entries, table has {len(rows)}"
            return "ok"
        except StorageError as exc:
            return str(exc)
        except Exception as exc:                     # any parse failure is corruption, not a crash
            return f"{type(exc).__name__}: {exc}"

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
        table.unique_sets.clear()      # a new column changes the shape the set was built for
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
    def _column_affinity(self, node, table):
        """Affinity of the column a comparison names, or None when the side is not a column."""
        if isinstance(node, ast.ColumnRef) and table is not None:
            position = table.index_of.get(node.name.lower())
            if position is not None:
                return table.columns[position].affinity
        return None

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
            return _order_sort_key(ascii_fold(value))
        return _order_sort_key(value)

    def _row_env(self, table, row, rowid=None, qualifier=None):
        env = {}
        prefix = (qualifier or table.name).lower()
        for column, value in zip(table.columns, row):
            env[column.name.lower()] = value
            env[f"{prefix}.{column.name.lower()}"] = value
        # The rowid namespace, unless the table declares a column of that name -- the reference
        # gives the real column precedence (t6d case L), so this must not overwrite one.
        if rowid is not None:
            for spelling in ROWID_SPELLINGS:
                if spelling not in env:
                    env[spelling] = rowid
                    env[f"{prefix}.{spelling}"] = rowid
        return env

    def _modified_table(self, node):
        if node.table.lower() in self.views and node.table.lower() not in self.tables:
            raise ExecError(f"cannot modify {node.table} because it is a view")
        return self.table(node.table)

    def _matching_rowids(self, table, where):
        """Rowids the access path can hand back for this predicate, or None when a scan is needed."""
        if where is None:
            return None
        decision = self._choose_access(table, where)
        if decision["access"] not in ("index", "rowid"):
            return None
        return decision["candidate"]["rowids"]

    def _update(self, node: ast.Update):
        table = self._modified_table(node)
        table.unique_sets.clear()      # a rewrite can change or remove any key
        positions = []
        for column, _ in node.assignments:
            position = table.index_of.get(column.lower())
            if position is None:
                raise ExecError(f"no such column: {column}")
            positions.append(position)
        if table.on_disk:
            wanted = self._matching_rowids(table, node.where)
            changed = 0
            source = ([(rowid, self._row_by_id(table, rowid)) for rowid in sorted(wanted)]
                      if wanted is not None else list(self._rows_with_ids(table)))
            for rowid, row in source:
                if row is None:
                    continue
                env = self._row_env(table, row, rowid)
                if node.where is not None and not truthy(self._eval(node.where, table, env)):
                    continue
                values = list(row)
                for (_, expression), position in zip(node.assignments, positions):
                    values[position] = apply_affinity(self._eval(expression, table, env),
                                                      table.columns[position].affinity)
                self._fire(table, "UPDATE", "BEFORE", row, values)
                self.storage.update_row(table.entry, rowid, values)
                self._fire(table, "UPDATE", "AFTER", row, values)
                self._maintain_indexes(table, rowid, row, values)
                changed += 1
            return changed

        wanted = self._matching_rowids(table, node.where)
        changed = 0
        if wanted is not None:
            source = [(rowid, self._row_by_id(table, rowid)) for rowid in sorted(wanted)]
        else:
            source = self._rows_with_ids(table)
        for rowid, row in source:
            if row is None:
                continue
            env = self._row_env(table, row, rowid)
            if node.where is not None and not truthy(self._eval(node.where, table, env)):
                continue
            values = list(row)
            # All assignments read the OLD row, so `SET a = b, b = a` swaps rather than copies.
            for (_, expression), position in zip(node.assignments, positions):
                values[position] = apply_affinity(self._eval(expression, table, env),
                                                  table.columns[position].affinity)
            self._fire(table, "UPDATE", "BEFORE", row, values)
            table.rows[self._rowid_index(table, rowid)] = tuple(values)
            self._fire(table, "UPDATE", "AFTER", row, values)
            self._maintain_indexes(table, rowid, row, values)
            changed += 1
        return changed

    def _delete(self, node: ast.Delete):
        table = self._modified_table(node)
        table.unique_sets.clear()      # a removed row frees its key
        if table.on_disk:
            kept = []
            changed = 0
            wanted = self._matching_rowids(table, node.where)
            for rowid, row in self._rows_with_ids(table):
                if wanted is not None and rowid not in wanted:
                    kept.append((rowid, row))
                    continue
                env = self._row_env(table, row, rowid)
                if node.where is not None and not truthy(self._eval(node.where, table, env)):
                    kept.append((rowid, row))
                else:
                    self._check_no_action(table, row)
                    changed += 1
            if changed:
                self.storage.rewrite_rows(table.entry, kept)
                table.entry["next_rowid"] = (max(rowid for rowid, _ in kept) + 1) if kept else 1
                self.storage.save_catalog_entry(table.entry)
            self._reindex(table)
            return changed
        kept = []
        kept_rowids = []
        changed = 0
        for rowid, row in self._rows_with_ids(table):
            env = self._row_env(table, row, rowid)
            if node.where is not None and not truthy(self._eval(node.where, table, env)):
                kept.append(row)
                kept_rowids.append(rowid)
            else:
                self._check_no_action(table, row)
                self._fire(table, "DELETE", "BEFORE", row, None)
                self._fire(table, "DELETE", "AFTER", row, None)
                changed += 1
        table.rows = kept
        table.rowids = kept_rowids
        # Allocation is max(rowid)+1, not a monotone counter: deleting the last rows returns the
        # counter to the reference's value (a full DELETE makes the next row 1 again, t6d case J).
        table.next_rowid = (max(kept_rowids) + 1) if kept_rowids else 1
        self._reindex(table)
        return changed

    def _maintain_indexes(self, table, rowid, old_values, new_values):
        """Incremental index upkeep: an UPDATE touches one row, so it must not rebuild the index."""
        for index in table.indexes.values():
            if self._index_applies(index, table, old_values):
                index.remove(rowid, old_values)
            if self._index_applies(index, table, new_values):
                index.add(rowid, new_values)

    def _reindex(self, table):
        for index in table.indexes.values():
            index.map = {}
            for rowid, values in self._rows_with_ids(table):
                if self._index_applies(index, table, values):
                    index.add(rowid, values)

    # -- SELECT --------------------------------------------------------------
    def _rowid_index(self, table, rowid):
        """Position of `rowid` in table.rows (memory tables only)."""
        if table.rowids is None:
            return rowid - 1
        return bisect.bisect_left(table.rowids, rowid)

    def _row_by_id(self, table, rowid):
        if table.on_disk:
            return self.storage.search_row(table.entry, rowid)
        if table.rowids is None:
            return table.rows[rowid - 1] if 0 < rowid <= len(table.rows) else None
        position = bisect.bisect_left(table.rowids, rowid)
        if position < len(table.rowids) and table.rowids[position] == rowid:
            return table.rows[position]
        return None

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

    def _rowid_equality(self, table, where):
        """The rowid a `rowid = <int>` or alias-column = <int> conjunct pins, or None.

        This is the access path the frozen acceptance prices: with an INTEGER PRIMARY KEY aliasing
        the rowid, a point lookup is one key rather than a table scan. A real column that happens
        to be called `rowid` is not the key, so it never reaches this path.
        """
        alias = self._rowid_alias_position(table)
        found = None

        def collect(node):
            nonlocal found
            if isinstance(node, ast.BinaryOp) and node.op == "AND":
                collect(node.left)
                collect(node.right)
            elif isinstance(node, ast.BinaryOp) and node.op == "=":
                left, right = node.left, node.right
                if isinstance(right, ast.ColumnRef) and isinstance(left, ast.Literal):
                    left, right = right, left
                if not (isinstance(left, ast.ColumnRef) and isinstance(right, ast.Literal)):
                    return
                name = left.name.lower()
                position = table.index_of.get(name)
                is_key = (position is None and name in ROWID_SPELLINGS) or \
                         (position is not None and position == alias)
                if is_key and isinstance(right.value, int) and not isinstance(right.value, bool):
                    found = right.value

        collect(where)
        return found

    def _predicate_texts(self, where):
        texts = set()

        def collect(node):
            if isinstance(node, ast.BinaryOp) and node.op == "AND":
                collect(node.left)
                collect(node.right)
            else:
                texts.add(_render_expression(node))

        if where is not None:
            collect(where)
        return texts

    def _ranges(self, table, where):
        """Range constraints usable by an index: conjunctions of `column OP literal`.

        Only the first index column can drive a hash-index range scan, so the planner uses these
        for single-column indexes. The literal is flipped when it appears on the left.
        """
        flipped = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
        ranges = {}

        def collect(node):
            if isinstance(node, ast.BinaryOp) and node.op == "AND":
                collect(node.left)
                collect(node.right)
            elif isinstance(node, ast.BinaryOp) and node.op in flipped:
                left, right = node.left, node.right
                op = node.op
                if isinstance(right, ast.ColumnRef) and isinstance(left, ast.Literal):
                    left, right = right, left
                    op = flipped[op]
                if isinstance(left, ast.ColumnRef) and isinstance(right, ast.Literal):
                    position = table.index_of.get(left.name.lower())
                    if position is not None:
                        ranges[position] = (op, right.value)

        collect(where)
        return ranges

    def _row_count(self, table):
        # No DELETE exists yet, so the next rowid is exactly the number of rows.
        return table.entry["next_rowid"] - 1 if table.on_disk else len(table.rows)

    def _plan_key(self, table, node):
        """Statement text + table + projection + predicates: the identity of a plan decision.

        The projection matters because covering is a property of it: a decision made for
        `SELECT a` must not be reused for `SELECT *`. Two subqueries on one table with different
        predicates must not share an entry either, and the rendered predicate texts are already
        the language partial indexes use for that.
        """
        if self._sql is None:
            return None
        predicates = (tuple(sorted(self._predicate_texts(node.where)))
                      if node.where is not None else ())
        projection = tuple("*" if isinstance(item, ast.Star) else _render_expression(item.expr)
                           for item in node.items)
        return (self._sql, table.name.lower(), projection, predicates)

    def _remember_plan(self, key, decision):
        if decision["access"] == "index" and len(decision["candidate"]["rowids"]) > PLAN_CACHE_ROWS:
            return                      # a wide lookup already paid; remembering it costs memory
        if len(self._plan_cache) >= PLAN_CACHE_MAX:
            self._plan_cache.clear()
        self._plan_cache[key] = decision

    @staticmethod
    def _unique_key(column, value):
        """The value as the constraint compares it: NOCASE folds strings, everything else is
        itself. Python equality already treats 1 and 1.0 as one key, which is SQLite's rule too."""
        if column.collation == "NOCASE" and isinstance(value, str):
            return ascii_fold(value)
        return value

    def _unique_bucket(self, table, position, column):
        bucket = table.unique_sets.get(position)
        if bucket is None:
            bucket = {self._unique_key(column, row[position]) for row in self._rows(table)}
            table.unique_sets[position] = bucket
        return bucket

    def _choose_access(self, table, where, node=None):
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
        ranges = self._ranges(table, where)
        rowid_value = self._rowid_equality(table, where)
        if rowid_value is not None:
            found = self._row_by_id(table, rowid_value) is not None
            rowids = {rowid_value} if found else set()
            candidate = {"index": None, "key": rowid_value, "rowids": rowids,
                         "estimated_rows": len(rowids), "kind": "rowid",
                         "columns": ["rowid"]}
            return {"access": "rowid", "reason": "rowid-equality", "candidates": [candidate],
                    "candidate": candidate, "estimated_rows": len(rowids)}
        candidates = []
        query_predicates = self._predicate_texts(where)
        for index in table.indexes.values():
            if index.where is not None and _render_expression(index.where) not in query_predicates:
                continue
            if all(position in equalities for position in index.columns):
                key = tuple(equalities[position] for position in index.columns)
                rowids = index.lookup(key)
                candidates.append({
                    "index": index,
                    "key": key,
                    "rowids": rowids,
                    "estimated_rows": len(rowids),
                    "kind": "equality",
                    "columns": [table.columns[i].name for i in index.columns],
                })
            elif len(index.columns) == 1 and index.columns[0] in ranges:
                op, value = ranges[index.columns[0]]
                rowids = index.lookup_range(op, value)
                if rowids:
                    candidates.append({
                        "index": index,
                        "key": None,
                        "rowids": rowids,
                        "estimated_rows": len(rowids),
                        "kind": "range",
                        "range": (op, value),
                        "columns": [table.columns[i].name for i in index.columns],
                    })

        summary = [{"name": c["index"].name, "columns": c["columns"],
                    "estimated_rows": c["estimated_rows"]} for c in candidates]
        if not candidates:
            return {"access": "full", "reason": "no-eligible-index", "candidates": summary,
                    "estimated_rows": total}

        best = min(candidates, key=lambda c: (c["estimated_rows"], -len(c["columns"]),
                                              not c["index"].unique, c["index"].name))
        # Cost model, measured rather than guessed: a random row fetch (~250 us) costs roughly
        # RANDOM_ACCESS_COST times a sequential row read (~10 us), so a range index only pays when
        # it touches a small fraction of the table. Equality keeps M5's simpler rule because an
        # exact bucket count is already the true row count there.
        #
        # A covering index pays no row fetch at all, so neither penalty applies to it: charging one
        # was the M22 boundary bug (the rule predates covering paths). This is the milestone's
        # measured correction — a 20x choice, not a 20x constant.
        covering = node is not None and self._covering_index(node, table, best["index"])
        if not covering and best["estimated_rows"] >= total:
            return {"access": "full", "reason": "index-not-selective", "candidates": summary,
                    "estimated_rows": total}
        if (not covering and best["kind"] == "range"
                and best["estimated_rows"] * RANDOM_ACCESS_COST >= total):
            return {"access": "full", "reason": "index-not-selective", "candidates": summary,
                    "estimated_rows": total}

        return {"access": "index", "reason": "smallest-estimate", "candidate": best,
                "candidates": summary, "estimated_rows": best["estimated_rows"],
                "covering": covering}

    # -- query execution -----------------------------------------------------
    AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})

    def _contains_aggregate(self, node):
        if isinstance(node, ast.FuncCall):
            if node.name == "JSON_GROUP_OBJECT":
                return len(node.args or []) == 2
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
                        "collations": {c.name.lower(): c.collation for c in table.columns},
                        "affinities": {c.name.lower(): c.affinity for c in table.columns},
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
                collations = {c.name.lower(): c.collation for c in table.columns}
                affinities = {c.name.lower(): c.affinity for c in table.columns}
            out.append({"alias": alias, "columns": columns, "rows": rows,
                        "collations": collations if join.select is None else {},
                        "affinities": affinities if join.select is None else {},
                        "kind": join.kind, "on": join.on})
        return out

    def _annotate_comparisons(self, expression, sources):
        """Annotate a predicate evaluated without a single table: collation and affinity.

        `_collation`/`_column_affinity` read their metadata off one Table, and a join or a
        multi-source scan has none -- so `ON t.a = u.a` compared BINARY for NOCASE columns (case
        21) and `WHERE n = '1'` skipped affinity (cases 19/20) whenever the complex path ran. The
        fix annotates a copy of the expression with what the sources declare: NOCASE columns get
        wrapped in COLLATE, and a literal compared against a column gets that column's affinity.
        """
        if expression is None:
            return None
        on = expression
        counts = {}
        for source in sources:
            for column in source["columns"]:
                counts[column.lower()] = counts.get(column.lower(), 0) + 1
        mapping, affinities = {}, {}
        for source in sources:
            for column, collation in source.get("collations", {}).items():
                if collation:
                    mapping[(source["alias"], column)] = collation
                    if counts.get(column) == 1:
                        mapping[column] = collation
            for column, affinity in source.get("affinities", {}).items():
                if affinity:
                    affinities[(source["alias"], column)] = affinity
                    if counts.get(column) == 1:
                        affinities[column] = affinity

        def column_key(ref):
            return (ref.table.lower(), ref.name.lower()) if ref.table else ref.name.lower()

        def walk(node):
            if isinstance(node, ast.ColumnRef):
                if mapping.get(column_key(node)) == "NOCASE":
                    return ast.Collate(expr=node, name="NOCASE")
                return node
            if isinstance(node, ast.BinaryOp) and node.op in ("=", "!=", "<", "<=", ">", ">="):
                # Affinity: SQLite converts the value a column is compared against.
                if isinstance(node.left, ast.ColumnRef) and isinstance(node.right, ast.Literal):
                    affinity = affinities.get(column_key(node.left))
                    if affinity is not None:
                        node.right = ast.Literal(apply_affinity(node.right.value, affinity),
                                                   node.right.kind)
                elif isinstance(node.right, ast.ColumnRef) and isinstance(node.left, ast.Literal):
                    affinity = affinities.get(column_key(node.right))
                    if affinity is not None:
                        node.left = ast.Literal(apply_affinity(node.left.value, affinity),
                                                  node.left.kind)
            for field in getattr(node, "__dataclass_fields__", ()):
                value = getattr(node, field, None)
                if isinstance(value, ast.Node):
                    setattr(node, field, walk(value))
                elif isinstance(value, list):
                    setattr(node, field, [walk(item) if isinstance(item, ast.Node) else item
                                          for item in value])
            return node

        return walk(copy.deepcopy(expression))

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
            join = self._annotate_comparisons(source["on"], sources)
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

    # -- name validation -----------------------------------------------------
    def _validate_select(self, select, outer=()):
        """Resolve every column name against the schema before a row is read.

        The gap this closes (STATE.md, Gap 2): names used to resolve while rows were evaluated, so
        an invalid query over an empty relation answered instead of erroring, and an unknown name
        inside an empty subquery became NULL. A name error is a schema fact, so it is decided here.

        Fail-open by design: a source whose columns cannot be enumerated statically (a view, a CTE
        whose body is not in hand, a table function) skips validation for that statement rather
        than guessing. The corpus is the over-rejection guard -- rejecting a valid query breaks
        hundreds of cases, accepting an invalid one breaks the few that name the error.
        """
        if isinstance(select, ast.Compound):
            self._validate_select(select.left, outer)
            self._validate_select(select.right, outer)
            return
        sources = self._validation_sources(select, outer)
        if sources is None:
            return
        aliases = {item.alias.lower() for item in select.items
                   if isinstance(item, ast.SelectItem) and item.alias}
        scope = tuple(sources)
        for item in select.items:
            if isinstance(item, ast.SelectItem):
                self._check_expr(item.expr, sources, aliases, outer)
        self._check_expr(select.where, sources, aliases, outer)
        for expression in select.group_by:
            self._check_expr(expression, sources, aliases, outer)
        self._check_expr(select.having, sources, aliases, outer)
        for expression, _descending in select.order_by:
            self._check_expr(expression, sources, aliases, outer, allow_alias=True)
        for join in select.joins:
            self._check_expr(join.on, sources, aliases, outer)
        if select.base_select is not None:
            self._validate_select(select.base_select, outer + (scope,))

    def _validation_sources(self, select, outer):
        """[(qualifier, column-name set)] for this select's FROM, or None if any is opaque."""
        sources = []

        def add(name, alias, derived):
            if derived is not None:
                self._validate_select(derived, outer)
                columns = self._derived_columns(derived)
                if columns is None:
                    return False
                sources.append(((alias or "").lower(), columns))
                return True
            table = self.tables.get(name.lower())
            if table is None:
                return False            # a view, a CTE, a table function: fail open
            sources.append(((alias or name).lower(), {c.name.lower() for c in table.columns}))
            return True

        if select.base_select is not None:
            if not add(None, select.alias, select.base_select):
                return None
        elif select.table is not None:
            if isinstance(select.table, ast.TableFunction):
                return None             # json_each and friends: columns exist only at run time
            if not add(select.table, select.alias, None):
                return None
        for join in select.joins:
            if join.select is not None:
                if not add(None, join.alias, join.select):
                    return None
            else:
                if not add(join.table, join.alias, None):
                    return None
        return sources

    def _derived_columns(self, select):
        if isinstance(select, ast.Compound):
            # A compound's headings come from its left arm; both arms are validated separately by
            # _validate_select, so only the names are needed here.
            return self._derived_columns(select.left)
        table = self.table(select.table) if select.table else None
        names = set()
        for item in select.items:
            if isinstance(item, ast.Star):
                if table is None:
                    return None
                names.update(column.name.lower() for column in table.columns)
                continue
            if not isinstance(item, ast.SelectItem):
                return None
            names.add((item.alias or _heading(item)).lower())
        return names

    def _check_column(self, node, sources, aliases, outer, allow_alias=False, allow_source=False):
        name = node.name.lower()
        if allow_source and not node.table and any(name == source_name for source_name, _ in sources):
            # `docs MATCH 'fox'`: the reference's full-text form names the table, not a column.
            return
        if node.table:
            qualifier = node.table.lower()
            for scope_sources in (sources,) + tuple(reversed(outer)):
                for source_name, columns in scope_sources:
                    if source_name != qualifier:
                        continue
                    if name in columns:
                        return
                    if name in ROWID_SPELLINGS and not any(
                            spelling.split(".")[-1] in columns for spelling in [name]):
                        return
                    raise ExecError(f"no such column: {node.table}.{node.name}")
            raise ExecError(f"no such column: {node.table}.{node.name}")
        matches = [columns for _source_name, columns in sources if name in columns]
        if len(matches) > 1:
            raise ExecError(f"ambiguous column name: {node.name}")
        if matches:
            return
        for scope_sources in tuple(reversed(outer)):
            counts = sum(1 for _source_name, columns in scope_sources if name in columns)
            if counts > 1:
                raise ExecError(f"ambiguous column name: {node.name}")
            if counts:
                return
        if allow_alias and name in aliases:
            return
        if name in ROWID_SPELLINGS and not any(
                name in columns for _source_name, columns in sources):
            return
        if getattr(node, "quote", "") == '"' and not node.table:
            # The reference reads a double-quoted name as an identifier that failed, and says so
            # with the string-literal hint; the resolver must not lose that text by raising first.
            raise ExecError(
                f'no such column: "{node.name}" - should this be a string literal in single-quotes?')
        raise ExecError(f"no such column: {node.name}")

    def _check_expr(self, node, sources, aliases, outer, allow_alias=False):
        if node is None:
            return
        if isinstance(node, ast.BinaryOp) and node.op.upper() == "MATCH":
            self._check_column(node.left, sources, aliases, outer, allow_alias, allow_source=True) \
                if isinstance(node.left, ast.ColumnRef) else self._check_expr(node.left, sources, aliases, outer)
            self._check_expr(node.right, sources, aliases, outer)
            return
        if isinstance(node, ast.ColumnRef):
            self._check_column(node, sources, aliases, outer, allow_alias)
            return
        if isinstance(node, ast.Subquery):
            self._validate_select(node.select, outer + (tuple(sources),))
            return
        if isinstance(node, ast.Exists):
            self._validate_select(node.select, outer + (tuple(sources),))
            return
        if isinstance(node, ast.InSubquery):
            self._check_expr(node.expr, sources, aliases, outer, allow_alias)
            self._validate_select(node.select, outer + (tuple(sources),))
            return
        if isinstance(node, (ast.Literal, ast.Star)):
            return
        for field_name in getattr(node, "__dataclass_fields__", ()):
            value = getattr(node, field_name, None)
            if isinstance(value, ast.Node):
                self._check_expr(value, sources, aliases, outer, allow_alias)
            elif isinstance(value, (list, tuple)):
                self._check_expr_list(value, sources, aliases, outer, allow_alias)

    def _check_expr_list(self, values, sources, aliases, outer, allow_alias):
        for value in values:
            if isinstance(value, ast.Node):
                self._check_expr(value, sources, aliases, outer, allow_alias)
            elif isinstance(value, (list, tuple)):
                self._check_expr_list(value, sources, aliases, outer, allow_alias)

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
        # With `FROM t AS x`, only the alias qualifies a name -- the reference refuses t.k -- so the
        # row environment is built under whichever name the statement actually gave the table.
        qualifier = node.alias or table.name
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

        plan_key = self._plan_key(table, node)
        remembered = self._plan_cache.get(plan_key) if plan_key is not None else None
        if remembered is not None:
            decision = remembered
        else:
            decision = self._choose_access(table, node.where, node=node)
            if plan_key is not None:
                self._remember_plan(plan_key, decision)
        if decision["access"] == "index" and self._covering_index(node, table, decision["candidate"]["index"]):
            chosen = decision["candidate"]
            index = chosen["index"]
            names = [name.lower() for name in index.column_names]
            prepared = []
            if chosen.get("key") is not None:
                buckets = [(chosen["key"], index.map.get(chosen["key"], set()))]
            else:
                op, value = chosen["range"]
                buckets = [(key, bucket) for key, bucket in index.map.items()
                           if len(key) == 1 and sql_compare(key[0], value, op)]
            for key, bucket in buckets:
                for rowid in bucket:
                    env = {}
                    for name, value in zip(names, key):
                        env[name] = value
                        env[f"{qualifier.lower()}.{name}"] = value
                    for spelling in ROWID_SPELLINGS:
                        if spelling not in env:
                            env[spelling] = rowid
                            env[f"{qualifier.lower()}.{spelling}"] = rowid
                    if node.where is not None and not truthy(self._eval(node.where, table, env)):
                        continue
                    prepared.append((env, tuple(env.get(name) for name in table.column_names)))
            prepared.sort(key=lambda pair: pair[1])
            decision = dict(decision)
            decision["reason"] = "covering-index"
            self.last_plan = {"table": table.name, "access": "index", "index": index.name,
                              "rows_examined": sum(len(b) for b in index.map.values()),
                              "rows_matched": len(prepared), "reason": "covering-index",
                              "estimated_rows": chosen["estimated_rows"],
                              "candidates": decision["candidates"],
                              "remembered": remembered is not None}
            rows = [tuple(self._eval(item.expr, table, env) for item in projection)
                    for env, _ in prepared]
            if node.distinct:
                rows = _distinct(rows)
            rows = self._apply_limit(node, rows)
            return items, rows

        if decision["access"] == "rowid":
            rowid = decision["candidate"]["key"]
            row = self._row_by_id(table, rowid)
            candidates = [(rowid, row)] if row is not None else []
        elif decision["access"] == "index":
            chosen = decision["candidate"]
            rowids = chosen["rowids"]
            if len(rowids) * 4 >= self._row_count(table):
                # Random-accessing a large share of the tree costs more than one sequential walk.
                candidates = [(rowid, row) for rowid, row in self._rows_with_ids(table)
                              if rowid in rowids]
            else:
                candidates = [(rowid, self._row_by_id(table, rowid)) for rowid in sorted(rowids)]
        else:
            candidates = list(self._rows_with_ids(table))
        access = decision["access"]
        examined = len(candidates)
        index_name = decision["candidate"]["index"].name if access == "index" else None

        prepared = []
        for rowid, row in candidates:
            if row is None:
                continue
            env = self._row_env(table, row, rowid, qualifier=qualifier)
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
                          "candidates": decision["candidates"],
                          "remembered": remembered is not None}
        return items, rows

    def _native_aggregate(self, node):
        """The single-table aggregate loop, in C (M38); None means "use the Python path".

        Only shapes whose meaning is unambiguous take this path: one base table, no WHERE, JOIN,
        HAVING, DISTINCT or ORDER BY, at most one GROUP BY column, and every projection item a
        plain aggregate over a column (or COUNT(*)) or the group key itself. Anything else — a
        scalar function inside SUM, a derived table, an expression argument — falls back, which is
        why the corpus cannot tell the difference by construction.
        """
        if (_native is None or node.base_select is not None or node.joins or node.having
                or node.distinct or node.where is not None or node.order_by
                or len(node.group_by) > 1 or not isinstance(node.table, str)):
            return None
        key_expression = node.group_by[0] if node.group_by else None
        if key_expression is not None and not isinstance(key_expression, ast.ColumnRef):
            return None
        table = self.table(node.table)
        positions = {column.name.lower(): position for position, column in enumerate(table.columns)}
        key_position = None
        if key_expression is not None:
            key_position = positions.get(key_expression.name.lower())
            if key_position is None:
                return None

        specs, layout, items = [], [], []
        for item in node.items:
            if not isinstance(item, ast.SelectItem):
                return None
            expression = item.expr
            items.append(item.alias or _heading(item))
            if (key_position is not None and isinstance(expression, ast.ColumnRef)
                    and expression.name.lower() == key_expression.name.lower()):
                layout.append(("key", None))
                continue
            if not isinstance(expression, ast.FuncCall) or getattr(expression, "distinct", False):
                return None
            if expression.star:
                if expression.name != "COUNT":
                    return None
                specs.append((0, -1))
                layout.append(("value", len(specs) - 1))
                continue
            if expression.name not in NATIVE_AGGREGATES or not isinstance(expression.arg, ast.ColumnRef):
                return None
            position = positions.get(expression.arg.name.lower())
            if position is None:
                return None
            specs.append((NATIVE_AGGREGATES[expression.name], position))
            layout.append(("value", len(specs) - 1))
        if not specs:
            return None

        rows_in = list(self._rows(table))
        grouped = _native.group_aggregate(rows_in, -1 if key_position is None else key_position,
                                          specs)
        if grouped is None:
            return None
        if key_position is None:
            entries = [(None, grouped.get(None, ()))]
        else:
            entries = sorted(grouped.items(), key=lambda entry: _group_sort_key((entry[0],)))

        out = []
        for key, values in entries:
            out.append(tuple(key if kind == "key" else values[position]
                             for kind, position in layout))
        out = self._apply_limit(node, out)

        alias = (node.alias or node.table).lower()
        self.last_plan = {
            "table": node.table,
            "tables": [alias],
            "access": "full",
            "sources": [{"table": alias, "rows": len(rows_in)}],
            "rows_examined": len(rows_in),
            "rows_matched": len(out),
            "reason": "aggregate",
            "index": None,
            "candidates": [],
            "native": True,
        }
        return items, out

    def _select_complex(self, node: ast.Select):
        """Joins, aggregates, GROUP BY/HAVING. Nested loop, planned and reported as such."""
        fast = self._native_aggregate(node)
        if fast is not None:
            return fast
        source_list = self._sources(node)
        envs, examined = self._scan_sources(node)
        if node.where is not None:
            predicate = self._annotate_comparisons(node.where, source_list)
            envs = [env for env in envs if truthy(self._eval(predicate, None, env))]

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
                    aliased = {}
                    for item in projection:
                        if isinstance(item, ast.SelectItem) and item.alias \
                                and not isinstance(item.expr, ast.Star):
                            aliased[item.alias.lower()] = self._eval(item.expr, None, env, group)
                    if isinstance(expression, ast.ColumnRef) and expression.name.lower() in aliased:
                        return _order_sort_key(aliased[expression.name.lower()])
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
        if node.name == "JSON_GROUP_OBJECT":
            pairs = {}
            for env in group:
                key = self._eval(node.args[0], None, env, group)
                value = self._eval(node.args[1], None, env, group)
                if key is None:
                    continue
                pairs[to_text(key)] = _json_input(value)
            return json_dump(pairs)
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

    def _scalar_subquery(self, select, env=None):
        saved = self.last_plan
        previous = self._outer_env
        if env is not None:
            self._outer_env = previous + (env,)
        try:
            _, rows = self._select_node(select)
        finally:
            self._outer_env = previous
            self.last_plan = saved
        if not rows:
            return None
        return rows[0][0]

    def _in_subquery(self, node, env, group):
        value = self._eval(node.expr, None, env, group)
        saved = self.last_plan
        previous = self._outer_env
        if env is not None:
            self._outer_env = previous + (env,)
        try:
            _, rows = self._select_node(node.select)
        finally:
            self._outer_env = previous
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
            return self._scalar_subquery(node.select, env)
        if isinstance(node, ast.Exists):
            saved_plan = self.last_plan
            previous = self._outer_env
            if env is not None:
                self._outer_env = previous + (env,)
            try:
                _, rows = self._select_node(node.select)
            finally:
                self._outer_env = previous
                self.last_plan = saved_plan
            return 1 if rows else 0
        if isinstance(node, ast.InList):
            value = self._eval(node.expr, table, env, group)
            if value is None:
                return None
            found_null = False
            for candidate_node in node.values:
                candidate = self._eval(candidate_node, table, env, group)
                if candidate is None:
                    found_null = True
                    continue
                if sql_compare(value, candidate, "="):
                    return not node.negated
            if found_null:
                return None
            return node.negated
        if isinstance(node, ast.InSubquery):
            return self._in_subquery(node, env, group)
        if isinstance(node, ast.FuncCall):
            # Aggregates must be resolved before their arguments are evaluated: over an empty
            # group there is no row to evaluate a column reference against.
            if node.name == "JSON_GROUP_OBJECT" and len(node.args or []) == 2:
                return self._aggregate(node, group)
            if node.name in self.AGGREGATES and len(node.args or []) <= 1:
                if group is None:
                    raise ExecError(f"misuse of aggregate function {node.name}()")
                return self._aggregate(node, group)
            arguments = [self._eval(argument, table, env, group) for argument in (node.args or [])]
            try:
                return call_function(node.name, arguments)
            except JsonError as exc:
                raise ExecError(str(exc)) from None
        if isinstance(node, ast.Raise):
            raise ExecError(node.message if node.message else node.action.lower())
        if isinstance(node, ast.ColumnRef):
            if node.table and self.trigger_env is not None:
                qualified = f"{node.table.lower()}.{node.name.lower()}"
                if qualified in self.trigger_env:
                    return self.trigger_env[qualified]
            key = (f"{node.table.lower()}.{node.name.lower()}" if node.table
                   else node.name.lower())
            if (env is None or key not in env) and self._outer_env:
                # Correlation: enclosing rows are consulted only when this query cannot resolve the
                # name itself, innermost scope first, so a name at any depth of nesting resolves.
                for scope in reversed(self._outer_env):
                    if key in scope:
                        return scope[key]
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
            if node.op == "MATCH":
                query = self._eval(node.right, table, env, group)
                if query is None:
                    return None
                columns = {}
                names = (table.column_names if table is not None
                         else [name for name in env if "." not in name])
                for column in names:
                    value = env.get(column)
                    columns[column.lower()] = fts_tokens(to_text(value)) if value is not None else []
                target = getattr(node.left, "name", "") or ""
                table_name = table.name.lower() if table is not None else ""
                if target.lower() in columns and target.lower() != table_name:
                    columns = {target.lower(): columns[target.lower()]}
                return fts_eval(fts_parse_query(to_text(query)), columns)
            if node.op in ("->", "->>"):
                left = self._eval(node.left, table, env, group)
                right = self._eval(node.right, table, env, group)
                if left is None or right is None:
                    return None
                try:
                    value = json_lookup(json_load(to_text(left)), json_path_steps(to_text(right)))
                except JsonError as exc:
                    raise ExecError(str(exc)) from None
                return json_to_text(value) if node.op == "->" else json_to_sql(value)
            nocase = (self._collation(node.left, table) == "NOCASE"
                      or self._collation(node.right, table) == "NOCASE")
            left = self._eval(node.left, table, env, group)
            right = self._eval(node.right, table, env, group)
            # SQLite applies the column's affinity to the value it is compared against, which is
            # why `n = '1'` matches integer 1 and `s = 1` matches the text '1'. Applied to the
            # literal side only: affinity is a property of the column, not of the comparison.
            if isinstance(node.right, ast.Literal):
                affinity = self._column_affinity(node.left, table)
                if affinity is not None:
                    right = apply_affinity(right, affinity)
            elif isinstance(node.left, ast.Literal):
                affinity = self._column_affinity(node.right, table)
                if affinity is not None:
                    left = apply_affinity(left, affinity)
            return sql_compare(left, right, node.op, nocase)
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
        # The three rowid spellings all report the heading `rowid` when qualified or not, because
        # they name one hidden column; an explicit AS still wins over the column's name.
        if item.expr.name.lower() in ROWID_SPELLINGS:
            return "rowid"
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
