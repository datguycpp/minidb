# Declared limits and known divergences

Every partial, declared scope-out, and known divergence in one table. A row exists so that a
reader never has to discover a boundary by hitting it: if the engine does not do something, this
file says so, says who decided, and says what breaks without it.

**What is not in here matters as much as what is.** Round 2's skeptic pass produced three engine
findings, and all three were fixed against pre-stated acceptances — the refusal-path readout that
reported `lag=0` on a drifted replica, the stale `<db>.vacuum.wal` sidecar, and the unlink window
in `resolve` that made the replica briefly absent. They are not rows because nothing about them is
still true. A reader should take the table as the current boundary, not as the residue of every
bug ever found.

Status values: **declared-out** (a deliberate scope boundary), **partial** (implemented for some
inputs only), **divergence** (behaves differently from SQLite on purpose or by omission).

| # | Item | What is missing | Owner | Why it matters |
|---|---|---|---|---|
| 1 | Exact output type of some error paths | error *text* matches the pinned shell; extended result codes are not modelled | M27 / corpus | anything comparing full CLI output at another SQLite version sees shell formatting, not SQL semantics (see `docs/reference-check.md`) |
| 2 | Collation set | `BINARY` and `NOCASE` (ASCII-only, as SQLite's is). `RTRIM` is *accepted but behaves as `BINARY`* — checked, not assumed: `'a  '` and `'a'` compare unequal under it. No ICU, no custom collations | engine | text that SQLite would treat as equal after trailing-space trimming compares bytewise here; anything beyond an ASCII case fold is bytewise too |
| 3 | `WITHOUT ROWID` tables | keyword rejected as a syntax error | declared-out | schemas written for SQLite that use it cannot be loaded; SQLite also enforces NOT NULL on such PKs, which is why the two differ together |
| 4 | Rowid writes | `UPDATE … SET rowid = N`, explicit rowid on an alias table, negative/zero/NULL rowids, AUTOINCREMENT interplay | t6d declared-out | tools that renumber rows by writing rowid fail with `no such column: rowid`; reads, holes, allocation and `VACUUM` renumbering are implemented |
| 5 | `PRIMARY KEY` nullability | matches SQLite's rowid tables (NULL allowed; an INTEGER PRIMARY KEY assigns) but not `WITHOUT ROWID`'s stricter rule | engine | a schema relying on PK ⇒ NOT NULL is not enforced here, exactly as in a rowid table |
| 6 | Covering scans with partial indexes; `ORDER BY` after projection on covering scans | not planned | M22 follow-up | a query that would avoid a table lookup does the lookup; results stay correct |
| 7 | `ANALYZE` / statistics | no pass, no `sqlite_stat*`; costing is rule-based, with a learned plan cache | M21 follow-up | plans for skewed data can be worse than the reference's; the plan cache learns from executed history instead |
| 8 | `BETWEEN` planning | planned as a scan rather than a two-bounded range | M21 follow-up | range predicates over large tables do more work than necessary |
| 9 | Concurrency model | single writer, redo log; WAL-mode non-blocking readers are not implied | M7 / M25 | concurrent writers see `database is locked`; `LOCK_SH` readers and one waiter are the supported shapes |
| 10 | Name validation coverage | resolver fails open for views, CTEs and table functions (their columns are not enumerated) | t6e | an invalid name inside a view body surfaces at execution, not at plan time |
| 11 | View/trigger body validation; `USING`/`NATURAL` inference | not validated, not inferred | t6e declared-out | a wrong joined column in `USING` is not caught statically |
| 12 | Replication merge policy | divergence is refused and resolved from the primary; there is no merge | M36 | two writers can never silently converge on a merged state — this is a policy, not a gap |
| 13 | Session-scoped learning | the plan cache lives in the session, not the file | M37 | a fresh process re-learns; no cross-process state to corrupt |
| 14 | Fuzzer dialect | 16 generated features | spec | parity claims cover the generated surface, not all of SQL |
| 15 | Crash coverage | `MINIDB_CRASH_POINT` sites + real `SIGKILL` sweeps; not every syscall boundary | tools/fuzz.py | a kill inside a path with no crash point is covered by the sweep, not by a named site |

## How rows are added and removed

- A finding becomes a row only when it is **not fixed**: a bug that is fixed leaves no row, and
  saying so (as above) is the honest form of "nothing was found".
- A row names an owner (the milestone or decision that scoped it out) and the consequence a user
  would hit, so a reader can judge relevance without reading the code.
- A row is deleted when the behaviour lands, and the removal is recorded in `LOG.md` with the
  commit that did it. A table that only grows is a table nobody trusts.
| 16 | Numeric literal forms | hexadecimal integer literals (`0x1f`) and digit-separator underscores (`1_000`) are rejected as `unrecognized token`; SQLite 3.54.0 returns 31 and 1000 | goal 6 lexer | schemas or queries using either spelling fail to run here and work in SQLite; decimal, exponent, leading/trailing-dot forms are supported |
