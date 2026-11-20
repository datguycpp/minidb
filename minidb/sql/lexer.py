"""Tokenizer for the MiniDB SQL subset.

Produces tokens carrying their source position so parse errors can point at the offending
character instead of a line number nobody can trust.
"""

import re

KEYWORDS = frozenset("""
ADD ALL ALTER AND AS ASC BEGIN BETWEEN BY CASE CAST CHECK COLLATE COMMIT CONSTRAINT CREATE
CROSS DEFAULT DELETE DESC DISTINCT DROP ELSE END EXCEPT EXISTS FOREIGN FROM FULL GROUP
HAVING IF IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY LEFT LIKE LIMIT NOT NULL OFFSET
ON OR ORDER OUTER RIGHT PRIMARY REFERENCES ROLLBACK SELECT SET TABLE THEN TRANSACTION TRUE FALSE
DEFERRED IMMEDIATE EXCLUSIVE
UNION UNIQUE UPDATE USING VALUES VIEW WHEN WHERE WITH RECURSIVE
OVER PARTITION ROWS RANGE PRECEDING FOLLOWING CURRENT UNBOUNDED ROW COLUMN RENAME TO TRIGGER PRAGMA VACUUM ANALYZE AUTOINCREMENT EXPLAIN QUERY PLAN BEFORE AFTER RAISE ABORT EACH MATCH VIRTUAL
""".split())

# Longest first: '<=' must win over '<'.
PUNCTUATION = sorted(
    [
        "->>", "->", "<=", ">=", "<>", "!=", "==", "||",
        "=", "<", ">", "+", "-", "*", "/", "%", "(", ")", ",", ";", ".",
    ],
    key=len,
    reverse=True,
)

# Alternation is first-match-wins and PUNCTUATION is sorted longest-first, so the list's
# own ordering is what preserves longest-match semantics. One C-level scan replaces up to 21
# startswith calls per punctuation token.
_NUM_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")  # exponent needs digits
_NUM_TAIL_RE = re.compile(r"[A-Za-z0-9_$]*")
_WS_RE = re.compile(r"[ \t\r\n\f\v]+")
_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

_PUNCT_BY_LEN = {}
for _p in PUNCTUATION:
    _PUNCT_BY_LEN.setdefault(len(_p), set()).add(_p)
_PUNCT_LENS = sorted(_PUNCT_BY_LEN, reverse=True)

IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
IDENT_BODY = IDENT_START | set("0123456789")


class LexError(Exception):
    """Carries SQLite's own wording: `unrecognized token: "<text>"`."""

    def __init__(self, message, pos, line, col, snippet=""):
        self.message = message
        self.pos = pos
        self.line = line
        self.col = col
        self.snippet = snippet
        super().__init__(f'unrecognized token: "{snippet}"' if snippet else message)


class Token:
    """A token. A plain slotted class, not a frozen dataclass: construction was 253 ms of the 697 ms
    a 10k-statement tokenize still costs, and the dataclass machinery (frozen __init__, per-field
    object.__setattr__, generated __eq__/__repr__) is the majority of it. Slots keep the memory
    shape and attribute speed; equality is spelled out because the reference streams are compared."""

    __slots__ = ("kind", "value", "pos", "line", "col", "raw", "quote", "_upper")

    def __init__(self, kind, value, pos, line, col, raw="", quote=""):
        self.kind = kind
        self.value = value
        self.pos = pos
        self.line = line
        self.col = col
        self.raw = raw
        self.quote = quote
        # The parser asks `tok.upper` on nearly every comparison -- 52 times per statement on a
        # plain SELECT — so it is computed once, here, for every kind (function and type names are
        # queried too).
        self._upper = value.upper() if isinstance(value, str) else value

    @property
    def upper(self):
        return self._upper

    def __eq__(self, other):
        if not isinstance(other, Token):
            return NotImplemented
        return (self.kind, self.value, self.pos, self.line, self.col, self.raw, self.quote) == (
            other.kind, other.value, other.pos, other.line, other.col, other.raw, other.quote)

    def __hash__(self):
        return hash((self.kind, self.value, self.pos, self.line, self.col, self.raw, self.quote))

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r}, {self.line}:{self.col})"


class _Scanner:
    def __init__(self, sql):
        self.sql = sql
        self.i = 0
        self.line = 1
        self.col = 1

    def peek(self, offset=0):
        j = self.i + offset
        return self.sql[j] if j < len(self.sql) else ""

    def advance(self, n=1):
        for _ in range(n):
            if self.i < len(self.sql):
                if self.sql[self.i] == "\n":
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.i += 1

    def skip(self, n):
        """Advance n characters known to contain no newline -- col only, one step.

        The char-at-a-time `advance` exists because newlines must update line/col as they pass;
        a run with no newline needs none of that bookkeeping, and identifiers/numbers can never
        contain one.
        """
        self.i += n
        self.col += n

    def skip_space_and_comments(self):
        while True:
            ch = self.peek()
            if ch in (" ", "\t", "\r", "\n", "\f", "\v"):
                chunk = _WS_RE.match(self.sql, self.i).group()
                newlines = chunk.count("\n")
                if newlines:
                    self.line += newlines
                    # col after consuming the run: 1-based position of the char following it.
                    self.col = len(chunk) - chunk.rfind("\n")
                else:
                    self.col += len(chunk)
                self.i += len(chunk)
            elif ch == "-" and self.peek(1) == "-":
                while self.peek() and self.peek() != "\n":
                    self.advance()
            elif ch == "/" and self.peek(1) == "*":
                start_line, start_col = self.line, self.col
                start_i = self.i
                self.advance(2)
                while True:
                    if not self.peek():
                        # SQLite treats an unterminated block comment as comment-to-EOF and
                        # carries on ("SELECT 1 /* never closed" returns 1). Match it.
                        return
                    if self.peek() == "*" and self.peek(1) == "/":
                        self.advance(2)
                        break
                    self.advance()
            else:
                return


# One C-level scan per token, with every construct this path cannot classify deterministically
# falling back to the character walk below -- the `_native` discipline applied to the lexer:
# a fast shape is only taken where equivalence is provable, and the walk remains the meaning.
_MASTER_RE = re.compile(
    r"(?P<ws>[ \t\r\n\f\v]+)"
    r"|(?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"|(?P<punct><=|>=|<>|!=|==|\|\||->>|->|[(),;.+\-*/%<>=])"
)
# Characters whose handling depends on context the regex does not model: quotes (contents are
# interpreted), `[` (quoted identifier), and the comment openers (which share a first character
# with punctuation).
_FALLBACK_CHARS = frozenset("'\"`[")


def _tokenize_regex(sql):
    """Regex fast path: identical output to the character walk, or `None` to request it.

    Returns `None` rather than raising for anything it cannot prove it would reproduce exactly --
    quoted text, comments, blobs, a number glued to identifier characters (whose error message
    names the whole run). The caller then takes the walk, so a shape this path gets wrong is a
    slower parse, never a different one.
    """
    tokens = []
    append = tokens.append
    i = 0
    n = len(sql)
    line = 1
    col = 1
    while i < n:
        ch = sql[i]
        if ch in _FALLBACK_CHARS:
            return None
        if ch == "-" and sql.startswith("--", i):
            return None
        if ch == "/" and sql.startswith("/*", i):
            return None
        match = _MASTER_RE.match(sql, i)
        if match is None:
            return None
        text = match.group()
        kind = match.lastgroup
        end = match.end()
        if kind == "ws":
            newlines = text.count("\n")
            if newlines:
                line += newlines
                col = len(text) - text.rfind("\n")
            else:
                col += len(text)
            i = end
            continue
        if kind == "num" and end < n and sql[end] in IDENT_START:
            return None
        append(Token("NUMBER" if kind == "num" else
                     "PUNCT" if kind == "punct" else
                     ("KEYWORD" if text.upper() in KEYWORDS else "IDENT"),
                     text, i, line, col, raw=text))
        col += len(text)
        i = end
    append(Token("EOF", "", i, line, col))
    return tokens


def tokenize(sql):
    fast = _tokenize_regex(sql)
    if fast is not None:
        return fast
    return _tokenize_chars(sql)


def _tokenize_chars(sql):
    sc = _Scanner(sql)
    tokens = []
    while True:
        sc.skip_space_and_comments()
        if not sc.peek():
            tokens.append(Token("EOF", "", sc.i, sc.line, sc.col))
            return tokens

        start_i, start_line, start_col = sc.i, sc.line, sc.col
        ch = sc.peek()

        # Quoted identifiers: "col", `col`, [col]
        if ch in ('"', "`", "["):
            close = "]" if ch == "[" else ch
            quote = ch
            sc.advance()
            buf = []
            while True:
                c = sc.peek()
                if not c:
                    raise LexError("unterminated quoted identifier", start_i, start_line, start_col,
                                   snippet=sc.sql[start_i:])
                if c == close:
                    # doubled quote escapes itself inside "..." and `...`
                    if close != "]" and sc.peek(1) == close:
                        buf.append(close)
                        sc.advance(2)
                        continue
                    sc.advance()
                    break
                buf.append(c)
                sc.advance()
            tokens.append(Token("IDENT", "".join(buf), start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i], quote=quote))
            continue

        # Blob literal: X'0102' (the X may be either case, SQLite allows both)
        if ch in ("x", "X") and sc.peek(1) == "'":
            sc.advance(2)
            digits = []
            while True:
                c = sc.peek()
                if not c:
                    raise LexError("unterminated blob literal", start_i, start_line, start_col,
                                   snippet=sc.sql[start_i:])
                if c == "'":
                    sc.advance()
                    break
                digits.append(c)
                sc.advance()
            hex_text = "".join(digits)
            if len(hex_text) % 2 or any(digit not in "0123456789abcdefABCDEF" for digit in hex_text):
                raise LexError("malformed database schema", start_i, start_line, start_col,
                               snippet=sc.sql[start_i:])
            value = bytes.fromhex(hex_text)
            tokens.append(Token("BLOB", value, start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i]))
            continue

        if ch == "'":
            sc.advance()
            buf = []
            while True:
                c = sc.peek()
                if not c:
                    raise LexError("unterminated string literal", start_i, start_line, start_col,
                                   snippet=sc.sql[start_i:])
                if c == "'":
                    if sc.peek(1) == "'":
                        buf.append("'")
                        sc.advance(2)
                        continue
                    sc.advance()
                    break
                buf.append(c)
                sc.advance()
            tokens.append(Token("STRING", "".join(buf), start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i]))
            continue

        if ch.isdigit() or (ch == "." and sc.peek(1).isdigit()):
            text = _NUM_RE.match(sc.sql, sc.i).group()
            sc.skip(len(text))
            # SQLite refuses a number immediately followed by an identifier character and names
            # the whole run in the message: `1e` -> unrecognized token: "1e", `2e+` -> "2e",
            # `1e5e5` -> "1e5e5" (verified against 3.54.0 and 3.51.0).
            nxt = sc.sql[sc.i:sc.i + 1]
            if nxt and nxt in IDENT_START:
                tail = _NUM_TAIL_RE.match(sc.sql, sc.i).group()
                raise LexError(f'unrecognized token: "{text}{tail}"', start_i, start_line, start_col,
                               snippet=text + tail)
            if text in (".",):
                raise LexError("malformed number", start_i, start_line, start_col, snippet=text)
            tokens.append(Token("NUMBER", text, start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i]))
            continue

        if ch in IDENT_START:
            word = _IDENT_RE.match(sc.sql, sc.i).group()
            sc.skip(len(word))
            kind = "KEYWORD" if word.upper() in KEYWORDS else "IDENT"
            tokens.append(Token(kind, word, start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i]))
            continue

        punct = ""
        for plen in _PUNCT_LENS:
            cand = sc.sql[sc.i:sc.i + plen]
            if cand in _PUNCT_BY_LEN[plen]:
                punct = cand
                break
        if not punct:
            raise LexError(f"unexpected character {ch!r}", start_i, start_line, start_col, snippet=ch)
        sc.advance(len(punct))
        tokens.append(Token("PUNCT", punct, start_i, start_line, start_col,
                            raw=sc.sql[start_i:sc.i]))


# --- Retained reference implementation -------------------------------------------------
# The char-at-a-time scanner, kept as the equivalence oracle for the fast path above.
# `tests/test_lexer_equivalence.py` compares the two on identical inputs; the M38 rule is
# that a fast path is only licensed by a slow path whose meaning is already established.
# NOTE: this is the pre-fix scanner, so it still accepts `2e+` and `1e5e5` -- the two
# divergences `0399c54` fixed. The equivalence test pins those separately rather than
# forcing the reference to share the fix.
class _ScannerRef:
    def __init__(self, sql):
        self.sql = sql
        self.i = 0
        self.line = 1
        self.col = 1

    def peek(self, offset=0):
        j = self.i + offset
        return self.sql[j] if j < len(self.sql) else ""

    def advance(self, n=1):
        for _ in range(n):
            if self.i < len(self.sql):
                if self.sql[self.i] == "\n":
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.i += 1

    def skip_space_and_comments(self):
        while True:
            ch = self.peek()
            if ch in (" ", "\t", "\r", "\n", "\f", "\v"):
                self.advance()
            elif ch == "-" and self.peek(1) == "-":
                while self.peek() and self.peek() != "\n":
                    self.advance()
            elif ch == "/" and self.peek(1) == "*":
                start_line, start_col = self.line, self.col
                start_i = self.i
                self.advance(2)
                while True:
                    if not self.peek():
                        # SQLite treats an unterminated block comment as comment-to-EOF and
                        # carries on ("SELECT 1 /* never closed" returns 1). Match it.
                        return
                    if self.peek() == "*" and self.peek(1) == "/":
                        self.advance(2)
                        break
                    self.advance()
            else:
                return


def _tokenize_scanner(sql):
    sc = _ScannerRef(sql)
    tokens = []
    while True:
        sc.skip_space_and_comments()
        if not sc.peek():
            tokens.append(Token("EOF", "", sc.i, sc.line, sc.col))
            return tokens

        start_i, start_line, start_col = sc.i, sc.line, sc.col
        ch = sc.peek()

        # Quoted identifiers: "col", `col`, [col]
        if ch in ('"', "`", "["):
            close = "]" if ch == "[" else ch
            quote = ch
            sc.advance()
            buf = []
            while True:
                c = sc.peek()
                if not c:
                    raise LexError("unterminated quoted identifier", start_i, start_line, start_col,
                                   snippet=sc.sql[start_i:])
                if c == close:
                    # doubled quote escapes itself inside "..." and `...`
                    if close != "]" and sc.peek(1) == close:
                        buf.append(close)
                        sc.advance(2)
                        continue
                    sc.advance()
                    break
                buf.append(c)
                sc.advance()
            tokens.append(Token("IDENT", "".join(buf), start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i], quote=quote))
            continue

        # Blob literal: X'0102' (the X may be either case, SQLite allows both)
        if ch in ("x", "X") and sc.peek(1) == "'":
            sc.advance(2)
            digits = []
            while True:
                c = sc.peek()
                if not c:
                    raise LexError("unterminated blob literal", start_i, start_line, start_col,
                                   snippet=sc.sql[start_i:])
                if c == "'":
                    sc.advance()
                    break
                digits.append(c)
                sc.advance()
            hex_text = "".join(digits)
            if len(hex_text) % 2 or any(digit not in "0123456789abcdefABCDEF" for digit in hex_text):
                raise LexError("malformed database schema", start_i, start_line, start_col,
                               snippet=sc.sql[start_i:])
            value = bytes.fromhex(hex_text)
            tokens.append(Token("BLOB", value, start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i]))
            continue

        if ch == "'":
            sc.advance()
            buf = []
            while True:
                c = sc.peek()
                if not c:
                    raise LexError("unterminated string literal", start_i, start_line, start_col,
                                   snippet=sc.sql[start_i:])
                if c == "'":
                    if sc.peek(1) == "'":
                        buf.append("'")
                        sc.advance(2)
                        continue
                    sc.advance()
                    break
                buf.append(c)
                sc.advance()
            tokens.append(Token("STRING", "".join(buf), start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i]))
            continue

        if ch.isdigit() or (ch == "." and sc.peek(1).isdigit()):
            buf = []
            seen_dot = False
            seen_exp = False
            while True:
                c = sc.peek()
                if c.isdigit():
                    buf.append(c)
                    sc.advance()
                elif c == "." and not seen_dot and not seen_exp:
                    seen_dot = True
                    buf.append(c)
                    sc.advance()
                elif c in ("e", "E") and not seen_exp and buf:
                    seen_exp = True
                    buf.append(c)
                    sc.advance()
                    if sc.peek() in ("+", "-"):
                        buf.append(sc.peek())
                        sc.advance()
                else:
                    break
            text = "".join(buf)
            if text[-1] in "eE" or text in (".",):
                raise LexError("malformed number", start_i, start_line, start_col, snippet=text)
            tokens.append(Token("NUMBER", text, start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i]))
            continue

        if ch in IDENT_START:
            buf = []
            while sc.peek() in IDENT_BODY:
                buf.append(sc.peek())
                sc.advance()
            word = "".join(buf)
            kind = "KEYWORD" if word.upper() in KEYWORDS else "IDENT"
            tokens.append(Token(kind, word, start_i, start_line, start_col,
                                raw=sc.sql[start_i:sc.i]))
            continue

        for punct in PUNCTUATION:
            if sc.sql.startswith(punct, sc.i):
                sc.advance(len(punct))
                tokens.append(Token("PUNCT", punct, start_i, start_line, start_col,
                                    raw=sc.sql[start_i:sc.i]))
                break
        else:
            raise LexError(f"unexpected character {ch!r}", start_i, start_line, start_col, snippet=ch)
