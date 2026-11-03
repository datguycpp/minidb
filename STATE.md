# MiniDB — STATE (Phase 2: hardening and honesty)

A SQL database engine scored against real SQLite. This file is the whole
handoff: a fresh bot continues from here alone. Full history lives in LOG.md
(moved verbatim at Phase-2 start; nothing there was edited in the move).

## Pinned reference
- SQLite CLI 3.54.0 (pinned in `tools/syntax_conformance.py` PINNED and here;
  change both together; the tool prints `*** PINNED IS ***` on drift).
- Box `sqlite3` module 3.53.1 is the timing-only reference (module-version skew
  accepted for timing, recorded in benchmark headers — the skew rule).
- Scores across reference versions are not comparable; a version change records
  old version, old score, new version, re-baselined score in LOG.md.

## Roles & operating rules
- Research: specs, corpus, fuzzer, benchmarks; holds skeptic veto ("not accepted"
  with a reproducible failing case — Default overrides only by fixing it or
  logging a declared limit). Never engine code.
- Engineer (build lane): code, one item at a time; never changes scope.
- Default (this seat): plans, backlog, ordering, review, ledger; never engine code.
- Drafts-first: tranche authored and parked before its build starts, always.
- Pre-register in LOG.md with timestamp before any benchmark/gate run: target +
  pass condition. Never change a threshold after a result; miss = miss.
- Gates anchor to real SQLite on the same machine (a multiple of the reference),
  never to the engine's own previous numbers.
- Fuzz reports print the literal all-mismatch count beside any filtered rate.
- Raw output only: failures are failures, never quietly patched and passed.
- Phase 2 (Steven, full freedom, no approvals): Default decides order/scope/
  approach, records why in LOG.md, keeps going. Safety: repo + tooling only, no
  history rewrites, no force-push; network only for installs + SQLite versions.

## Latest score
- Corpus 293/293 (100%), 554 tests, syntax 46/46, docs examples 7/7.
- Prediction tally: 13 in-band / 3 under / 7 generous over 23 bands.
- Benchmarks, 100k (N=7 unless noted): W1 16.9x (57µs/lookup, ceil 500µs), W8
  88x (1.37ms/update, ceil 5ms), W3 0.928 sub-1, W6 0.219, W7 59.5, W9 354x
  (N=5; on-disk scan ~411ms vs 8.37ms in-memory — read-path item un-ordered).
- Crash sweep 12/12 sites, 2400 crashes, 0 failures; replication byte-identical
  incl. renumbering-VACUUM rider (check 8); 100k pair gated + paired.

## Current status
- Tier-6 CLOSED. Post-close items LANDED: rowid aliasing (f0ef025), Gap 2
  resolver (d46618e), W9 in default list (7ec0c1f). Read-path brief measured,
  UN-ORDERED (docs/readpath-prework.md).
- Skeptic round 1 CLOSED on fix side (31/32; only case 30 declared-out remains;
  6 findings fixed e8aaba2, cold-verified; 562 tests. Triage corrected case 16
  direction: NOCASE over-folds non-ASCII → narrowed to ASCII). Charter note: a
  force-push by the build lane (own commit only, parent intact) is recorded as
  a boundary violation with no exception — the compliant move was LOG note.
  Round 2 pre-registered (SIGKILL VACUUM, concurrency, replication probes).

## Open items
- Read-path order decision: scope (a) COUNT-shaped (floor ~19ms, gate computed),
  scope (b) every on-disk scan (target declared with shape), or neither.
- Phase-2 goals below are Default-ordered and need no approval.

## Declared limits (item | what is missing | owner | why it matters)
- WITHOUT ROWID tables, index b-tree pages (foreign files) | outcome-parity via
  schema-text rebuild; cost differs | M23 | format scope, never promised.
- Hot `-wal` foreign files invisible | crashed-file WAL frames | M23/M27-adjacent
  item | recorded scope note, tranche when M27-adjacent work orders.
- Writing SQLite's format | out of scope; read-gate is read-only | spec | —.
- Covering-for-partial indexes; ORDER BY after projection on covering scans | — |
  M22 | recorded, not hidden.
- ANALYZE pass; BETWEEN plans as scan | stats-based costing; two-bounded ranges |
  M21 follow-ups | declared scope-outs.
- Single-writer redo log; WAL-mode non-blocking NOT implied | concurrent-write
  shapes beyond LOCK_SH readers + waiter | M7/M25 | rollback-journal model.
- Resolver fail-open: views, CTEs, table functions unvalidated | name errors
  there | t6e | rejecting valid queries is the worse class.
- View/trigger-body validation; USING/NATURAL inference | — | t6e declared-out | —.
- Rowid writes: UPDATE SET rowid, explicit rowid on alias tables, negative/zero/
  NULL rowids, AUTOINCREMENT interplay | — | t6d declared-out | —.
- Fuzzer dialect = 16 features | wider surface unfuzzed | spec | declared.
- Error-text expectations are shell-version-specific (14/293 cases under a non-pinned
  SQLite shell) | corpus scores comparable only at the pinned 3.54.0 | goal 4 /
  `docs/reference-check.md` | the `(NN)` suffix + quote-hint are shell formatting, not
  SQL semantics; normalizing was declined on purpose (it would loosen the pinned oracle).

## Next tasks (Phase-2 order, reasons in LOG.md)
1. ~~`make verify`~~ **DONE** — `make verify` runs build + tests + corpus + crash
   sweep + docs and prints one summary block with raw counts and the revision;
   verified from a fresh clone (54 s, 5/5) and its failure path on a labelled
   fixture clone (exit 1, mismatch line named).
2. Skeptic round (Research adversarial): each finding becomes a corpus case or a
   LIMITS.md entry; zero findings explained.
3. docs/LIMITS.md: every partial/limit/divergence in one table (extends above).
4. ~~Independent reference check~~ **DONE** (a33872d; corpus vs 3.51.0 + 3.45.1:
   13/14 oracle-moved, MiniDB matches pinned on all, 0 genuine; fuzz seeds 1,7
   clean on all three oracles; cold re-run vs 3.51.0 reproduces 13/0. Declared
   limit: error text is shell-version-specific, scores comparable at 3.54.0).
5. Performance: hot paths (on-disk scan, per-statement parsing) in the judged-best
   toolchain; parity rules hold (same corpus, same fuzz counts on same seeds).

## How to verify
- **`make verify`** — one command, one summary block, raw counts, git revision.
  Stages: build (optional C accelerator; SKIP without a compiler), tests, corpus,
  crash sweep (`--iterations`, default 100), docs examples. Exit 0 only when every
  stage passes; a failing stage prints FAIL, keeps its counts, and reproduces its
  raw failure output below the table.
- The same stages individually:
  `python3 -m unittest discover -s tests` (554 green).
- `python3 tools/behavior_conformance.py` (293/293).
- `python3 tools/replication_check.py` (tail: 12/12 sites, 0 failures).
- `python3 tools/docs_runner.py` (docs examples; M33 bidirectional drift assertion).
- Benchmarks: `python3 tools/benchmark.py --scale 100000 --n 7` (hours; W1/W8
  changed the curve — first block is W1's scans; keep the file alive, snapshot
  to ~/Documents/minidb-evidence/).
