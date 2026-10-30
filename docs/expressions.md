# Expression semantics (milestone 10)

Where SQLite's rules differ from the obvious language defaults, and why each one is written this
way. Every rule below is covered by `tests/test_expressions.py`, which runs both engines on the
same input rather than asserting what we think SQLite does.

## Arithmetic

| Expression | SQLite | Note |
| --- | --- | --- |
| `-7 / 2` | `-3` | integer division **truncates toward zero**, unlike Python's `//` |
| `-7 % 2` | `-1` | remainder's sign follows the **dividend** |
| `7 % -2` | `1` | same rule, other direction |
| `1 / 0` | `NULL` | division by zero is NULL, not an exception |
| `5 / 2.0` | `2.5` | either operand REAL promotes the result |
| `'5' + 1` | `6` | text takes its **leading numeric prefix** |
| `'abc' + 1` | `1` | no numeric prefix means 0 |
| `1 \|\| 'x'` | `'1x'` | concatenation formats numbers as text |
| `2.0 \|\| 'x'` | `'2.0x'` | integral REAL keeps one decimal place |
| `'a' \|\| NULL` | `NULL` | concatenation propagates NULL |

## Truthiness

`WHERE` and `AND`/`OR` use SQLite's notion, not Python's: `0` and `''` are false, any other
number is true, text is true when its numeric prefix is non-zero (`'1'` true, `'abc'` false),
`NULL` is unknown. Three-valued logic follows from that:

| Expression | Result |
| --- | --- |
| `NULL AND 0` | `0` (a false operand decides) |
| `NULL AND 1` | `NULL` |
| `NULL OR 1` | `1` |
| `NULL OR 0` | `NULL` |
| `NOT NULL` | `NULL` |
| `NULL = NULL` | `NULL` |
| `NULL IS NULL` | `1` — `IS` is NULL-safe and never returns NULL |

## CASE and CAST

- `CASE WHEN p THEN x ... [ELSE y] END` — a missing `ELSE` yields NULL, and the first matching
  branch wins.
- Simple `CASE operand WHEN value ...` compares with `IS` semantics, so a NULL operand matches a
  NULL `WHEN`.
- `CAST('abc' AS INTEGER)` is `0`; `CAST('12abc' AS INTEGER)` is `12`; `CAST(3.9 AS INTEGER)` is
  `3` (truncation toward zero). Text→REAL behaves the same way with a float result.

## Not here yet

Built-in functions (`abs`, `length`, `substr`, `date`, …) are M11; `ORDER BY`/`LIMIT` are M12;
set operations are M13; window functions M15. `COUNT(DISTINCT x)` **is** implemented, since it is
an aggregate rather than a function.
