"""Tokenizer for the MiniDB SQL subset.

Produces tokens carrying their source position so parse errors can point at the offending
character instead of a line number nobody can trust.
"""

from dataclasses import dataclass

KEYWORDS = frozenset("""
ADD ALL ALTER AND AS ASC BETWEEN BY CASE CAST CHECK COLLATE COMMIT CONSTRAINT CREATE
CROSS DEFAULT DELETE DESC DISTINCT DROP ELSE END EXCEPT EXISTS FOREIGN FROM FULL GROUP
HAVING IF IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY LEFT LIKE LIMIT NOT NULL OFFSET
ON OR ORDER OUTER PRIMARY REFERENCES SELECT SET TABLE THEN TRANSACTION TRUE FALSE
UNION UNIQUE UPDATE USING VALUES VIEW WHEN WHERE
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
    def __init__(self, message, pos, line, col):
        super().__init__(f"{message} at line {line}, column {col}")
        self.message = message
        self.pos = pos
        self.line = line
        self.col = col


@dataclass(frozen=True)
class Token:
    kind: str          # KEYWORD | IDENT | NUMBER | STRING | PUNCT | EOF
    value: str         # raw text as written (identifiers keep their case)
    pos: int
    line: int
    col: int

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
                self.advance(2)
                while True:
                    if not self.peek():
                        raise LexError("unterminated block comment", self.i, start_line, start_col)
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
            sc.advance()
            buf = []
            while True:
                c = sc.peek()
                if not c:
                    raise LexError("unterminated quoted identifier", start_i, start_line, start_col)
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
            tokens.append(Token("IDENT", "".join(buf), start_i, start_line, start_col))
            continue

        if ch == "'":
            sc.advance()
            buf = []
            while True:
                c = sc.peek()
                if not c:
                    raise LexError("unterminated string literal", start_i, start_line, start_col)
                if c == "'":
                    if sc.peek(1) == "'":
                        buf.append("'")
                        sc.advance(2)
                        continue
                    sc.advance()
                    break
                buf.append(c)
                sc.advance()
            tokens.append(Token("STRING", "".join(buf), start_i, start_line, start_col))
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
            tokens.append(Token("NUMBER", "".join(buf), start_i, start_line, start_col))
            continue

        if ch in IDENT_START:
            buf = []
            while sc.peek() in IDENT_BODY:
                buf.append(sc.peek())
                sc.advance()
            word = "".join(buf)
            kind = "KEYWORD" if word.upper() in KEYWORDS else "IDENT"
            tokens.append(Token(kind, word, start_i, start_line, start_col))
            continue

        for punct in PUNCTUATION:
            if sc.sql.startswith(punct, sc.i):
                sc.advance(len(punct))
                tokens.append(Token("PUNCT", punct, start_i, start_line, start_col))
                break
        else:
            raise LexError(f"unexpected character {ch!r}", start_i, start_line, start_col)
