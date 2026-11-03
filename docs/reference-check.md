# Independent reference check (goal 4)

The corpus's expected results are computed at run time by the pinned `sqlite3` shell (3.54.0,
Arch build). This check runs every case against two other SQLite builds and separates two
questions that are easy to confuse:

- *did the oracle move?* — do the two SQLite builds disagree on a case;
- *is MiniDB wrong?* — does MiniDB fail to match the pinned build, the alternate build, or both.

Run it yourself:

```
python3 tools/reference_check.py --alternate /path/to/sqlite3
MINIDB_SQLITE=/path/to/sqlite3 python3 tools/behavior_conformance.py   # the plain score line
MINIDB_SQLITE=/path/to/sqlite3 python3 tools/fuzz.py --seed 1 --queries 1000
```

`MINIDB_SQLITE` is the supported override; unset, everything means what it always meant.

## Builds used

| build | date | source |
|---|---|---|
| 3.54.0 pinned (Arch `sqlite 3.54.0-1`) | 2026-10-09 | `sqlite3` on PATH |
| 3.54.0 upstream prebuilt | 2026-10-09 | sqlite.org `sqlite-tools-linux-x64-3540000.zip` |
| 3.51.0 upstream prebuilt | 2025-11-04 | sqlite.org `sqlite-tools-linux-x64-3510000.zip` |
| 3.45.1 upstream prebuilt | 2024-01-30 | sqlite.org `sqlite-tools-linux-x64-3450100.zip` |

The upstream 3.54.0 build was fetched specifically to separate a packaging difference from an
upstream one: on every case it produces the same text as the Arch binary, so the differences
below are upstream shell behaviour, not Arch's build flags.

## What moved

293 cases (the behaviour harness's own file set; `syntax.txt` belongs to the syntax harness and is
excluded, otherwise the totals disagree with the score line).

| alternate | cases | oracle moved | MiniDB matches pinned | genuine findings |
|---|---|---|---|---|
| 3.51.0 | 293 | 13 | 13/13 | 0 |
| 3.45.1 | 293 | 14 | 14/14 | 0 |

Every movement is in **error text**, never in rows or column headings:

1. **The result-code suffix `(NN)` (13 cases).** 3.51.0 and 3.45.1 shells append the extended
   result code to constraint errors:

   ```
   3.51.0 / 3.45.1 : Runtime error near line 1: NOT NULL constraint failed: t.name (19)
   3.54.0 pinned   : Error near line 1: NOT NULL constraint failed: t.name
   ```
   Both the prefix ("Runtime error" → "Error") and the suffix changed in the shell between
   3.51.0 and 3.54.0. The SQL error itself is identical; only the shell's presentation moved.

2. **The double-quoted-identifier hint (1 case, t1a case 6).** 3.45.1 says
   `no such column: apple`; 3.51.0 and 3.54.0 say
   `no such column: "apple" - should this be a string literal in single-quotes?`.
   The hint was added between 3.45.1 and 3.51.0 and is stable since.

No case changed its *data*: no row, column heading, ordering, or type differs between any two
builds. That is the useful negative result — the differential surface MiniDB is written against
(rows and values) is version-independent across three years of SQLite releases.

## Fuzzer

The same seeds under all three oracles, counting **every** mismatch (no filtered or "semantic"
rate):

| seed / queries | 3.54.0 pinned | 3.51.0 | 3.45.1 |
|---|---|---|---|
| seed 1 / 1000 | 0 mismatches | 0 mismatches | 0 mismatches |
| seed 7 / 500 | 0 mismatches | 0 mismatches | 0 mismatches |

The fuzzer's generated surface never produced a version-sensitive error text, so parity holds
across oracles on it. This is the literal all-mismatch count, not a rate over a subset.

## What this means, and the one recommendation

The corpus is a **pinned-shell** oracle, and 14 of its 293 cases encode that shell's error
formatting rather than SQL semantics. Against a different build those cases look like failures
when nothing about MiniDB's answers has changed.

- Declared limit (for `docs/LIMITS.md`): error-text expectations are shell-version-specific; the
  corpus score is only comparable at the pinned 3.54.0.
- Not changed here, deliberately: normalizing the `(NN)` suffix in the harness would make the
  corpus version-robust, but it would also loosen the pinned reference the project chose on
  purpose. Recording the limit is the honest move; changing the oracle's strictness is a spec
  decision, not a tooling one.
