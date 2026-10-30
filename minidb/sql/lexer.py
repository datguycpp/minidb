"""Tokenizer for the MiniDB SQL subset.

Produces tokens carrying their source position so parse errors can point at the offending
character instead of a line number nobody can trust.
"""

from dataclasses import dataclass

KEYWORDS = frozenset("""
ADD ALL ALTER AND AS ASC BEGIN BETWEEN BY CASE CAST CHECK COLLATE COMMIT CONSTRAINT CREATE
CROSS DEFAULT DELETE DESC DISTINCT DROP ELSE END EXCEPT EXISTS FOREIGN FROM FULL GROUP
HAVING IF IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY LEFT LIKE LIMIT NOT NULL OFFSET
ON OR ORDER OUTER RIGHT PRIMARY REFERENCES ROLLBACK SELECT SET TABLE THEN TRANSACTION TRUE FALSE
DEFERRED IMMEDIATE EXCLUSIVE
UNION UNIQUE UPDATE USING VALUES VIEW WHEN WHERE WITH RECURSIVE
OVER PARTITION ROWS RANGE PRECEDING FOLLOWING CURRENT UNBOUNDED ROW COLUMN RENAME TO TRIGGER PRAGMA VACUUM ANALYZE AUTOINCREMENT BEFORE AFTER RAISE ABORT EACH
""".split())

# Longest first: '<=' must win over '<'.
PUNCTUATION = sorted(
    [
        "<=", ">=", "<>", "!=", "==", "||",
        "=", "<", ">", "+", "-", "*", "/", "%", "(", ")", ",", ";", ".",
    ],
    key=len,
    reverse=True,
)

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


@dataclass(frozen=True)
class Token:
    kind: str          # KEYWORD | IDENT | NUMBER | STRING | PUNCT | EOF
    value: str         # interpreted text (string contents, unquoted identifier)
    pos: int
    line: int
    col: int
    raw: str = ""      # exact source text, for error messages SQLite-style
    quote: str = ""    # '"', "`", "[" when the identifier was quoted

    @property
    def upper(self):
        return self.value.upper()

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


def tokenize(sql):
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
