# Reference inventory (Tier-6 entry)

Run before any Tier-6 implementation, so the stretch claims are written against what the pinned
binary actually carries rather than what its documentation advertises.

**Binary:** `sqlite3 --version` → `3.54.0 2026-10-09 15:46:58 be8d059e9a49089ab2dce5ed26dd87aaf598fdc5fbe0b107c0dd758464bcalt1 (64-bit)`

## Probes

```
$ sqlite3 :memory: "PRAGMA compile_options;" | grep -Ei "json|fts|math|rtree|stat4"
ENABLE_FTS3
ENABLE_FTS3_PARENTHESIS
ENABLE_FTS4
ENABLE_FTS5
ENABLE_MATH_FUNCTIONS
ENABLE_RTREE
ENABLE_STAT4

$ sqlite3 :memory: "SELECT json('[1]'), json_extract('[1,2]','$[0]');"
[1]|1

$ sqlite3 :memory: "CREATE VIRTUAL TABLE t USING fts5(a); INSERT INTO t VALUES('x'); SELECT * FROM t WHERE t MATCH 'x';"
x

$ sqlite3 :memory: "SELECT sin(0), pow(2,10), abs(-3);"
0.0|1024.0|3
```

## Result

```
REFERENCE-INVENTORY: json1=yes fts5=yes math=yes rtree=yes stat4=yes
  probes: PRAGMA compile_options; json1 smoke; fts5 smoke; math smoke
```

## The finding that matters

**`PRAGMA compile_options` does not list `ENABLE_JSON1` on this build, and JSON works anyway.**
JSON functions have been compiled in unconditionally since SQLite 3.38, so the pragma line is
absent while `json()` and `json_extract()` execute correctly. An inventory that grepped the pragma
for `ENABLE_JSON1` would have concluded *no json1* and written M35 off as "our own syntax, no
differential" — against a reference that supports it fully.

That is the spec's own warning ("a pragma line and a working feature are two different facts")
firing in the opposite direction from the one it was written for: the smoke query is not a
belt-and-braces duplicate of the pragma, it is the authoritative check, and the pragma is the
hint. Both are recorded here so the next inventory starts from the right one.

## Consequences for Tier 6

- **M34 (full-text search):** fts5 present → differential conformance against the declared subset
  (`MATCH` with plain terms and AND/OR/NOT), virtual-table syntax in the parser.
- **M35 (JSON):** json1 present → the differential tranche is live; core functions are
  `json`, `json_extract`, `json_array`, `json_object`, `json_type`, `json_valid`, `json_quote`,
  `json_array_length`, plus the `->` and `->>` operators.
- **Math functions:** present, and already handled at M11's ruling — in scope, differential.
