# Tier-6 prework (owner: Research) — stretch milestones M34–M38

Last authored spec; written before Tier 5 ends. Tier 6 is open-ended by
design, so the value here is fixed acceptance shapes and one standing
ruling carried forward, not case lists.

## Standing ruling carried into Tier 6: the reference build defines the surface

Ratified at the M11 math-functions finding: **the pinned binary's compiled-
in features are what conformance requires** — optional modules the build
ships are in scope, optional modules it lacks are out (both by decision 3).
Tier 6's FTS and JSON claims therefore begin with a **runtime feature
inventory** at Tier-6 entry, printed in the first report (exact probe commands, so entry is one
command, not an improvisation):

```
sqlite3 :memory: "PRAGMA compile_options;"     # ENABLE_JSON1 / ENABLE_FTS5 / ENABLE_MATH_FUNCTIONS …
sqlite3 :memory: "SELECT json('[1]'), json_extract('[1,2]','$[0]');"   # json1 smoke
sqlite3 :memory: "CREATE VIRTUAL TABLE t USING fts5(a); INSERT INTO t VALUES('x'); SELECT * FROM t WHERE t MATCH 'x';";  # fts5 smoke
```

`PRAGMA compile_options` is authoritative for what the build carries;
the smoke queries confirm the pragma's claims actually execute (a pragma
line and a working feature are two different facts — verify both).

```
REFERENCE-INVENTORY: json1=<yes/no> fts5=<yes/no> math=<yes/no> … (probe commands attached)
```

If the probe says the pinned build has `json_extract` and `fts5`, the
ladder's stretch lines are live conformance claims against them; if it
says no, the milestones become *feature claims of our own* with the
reference silent — recorded either way **before** implementation starts,
never after.

## M34 — full-text search

- If fts5 is present: conformance is against the fts5 *query syntax we
  declare* (a declared subset — `MATCH` with plain terms and AND/OR/NOT —
  full fts5 grammar is a bottomless scope; subset listed in the milestone
  report, cases authored by Research when the milestone approaches).
- Virtual-table machinery (`CREATE VIRTUAL TABLE … USING fts5`) is the
  parser surface; ranking functions (`bm25`) are subset-listed.
- If fts5 is absent from the build: our own syntax, documented as ours;
  acceptance = usefulness + docs-runner examples, no differential.

## M35 — JSON functions

- Same inventory-first logic. JSON1's core (`json`, `json_extract`,
  `json_array`, `json_object`, `json_type`, `json_valid`) is small,
  deterministic and **differentially ideal** — expect a normal Research
  tranche authored pre-milestone with a normal prediction band.
- Path syntax and error text for bad paths are the rule-shaped swing,
  like every error-text case before it.

## M36 — replication to a second instance

- **No reference exists; invariant acceptance only** (the M25 class):
  committed-then-replicated state identical on both ends; divergence after
  partition healed per the declared policy (last-writer-wins or reject —
  policy stated in the milestone, chosen before testing); replication lag
  never yields a torn logical row (all-or-nothing per transaction).
- Deterministic tests drive the replica through scripted partitions
  (seeded, replayable — M24's seed discipline), not timing races.

## M37 — planner that learns from query history

- **Outputs are the conformance surface, choices are not** (the M21/EXPLAIN
  class): corpus must stay green regardless of what the planner learns;
  plan_check's existing riders (right answer, declared access path,
  estimated vs actual) plus a before/after benchmark delta on repeated
  query sets is the acceptance. A learned planner that changes answers
  fails even if its plans get faster.
- Guard: learning state is part of the database file → M23's file
  compatibility question recurs (foreign file + learning state); flagged
  for the milestone, decided there.

## M38 — rewrite a hot path in a faster language, prove parity

- Acceptance is mechanical and pre-agreed: **full corpus unchanged
  (identical per-file lines before/after), fuzz run at equal seeds
  unchanged (same mismatches, same count — a fuzz delta is a behavior
  change, not a rewrite), benchmark W1–W10 ratios improved or equal**,
  with before/after numbers from the same protocol (warm-up, N=7, medians).
- Binary/dependency additions must be declared (build portability is part
  of "clean-machine install" from M33's gate family); a rewrite that needs
  a toolchain the box doesn't have trades one gate for another.

## What stays open until approached

Prediction bands for M34–M38 get registered when their milestones
approach (with the engine state measurable), and Research's tranche for
M34/M35 is authored at Tier-6 entry — same sequence as every tier so far.
