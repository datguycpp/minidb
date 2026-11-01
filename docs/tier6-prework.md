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

- **No reference exists; invariant acceptance only** (the M25 class).
- **Divergence policy: REJECT, declared before testing** (per this spec's
  own rule — chosen ahead of the build, not discovered during it): a ship
  whose base seq does not match the replica's state is refused with a
  typed error naming the base seq; no merge is attempted. Recorded
  rationale: last-writer-wins over batch/page shipping is whole-page
  overwrite — a committed row vanishes under a structurally valid file
  (`integrity_check` cannot see row-level absence); true row-level LWW
  needs per-row versions (file format v3) and a logical clock, and wall
  clock is disqualified by our own `date('now')` SGT finding. LWW would
  also silently discard same-key losers *by design*, which the M27 ruling
  (silent wrong data fails) does not permit.
- **Quantified identity invariant** (the M25/M30 discipline: unreachable
  as plain prose, replaced in writing): *whenever a ship succeeds, the
  replica file is byte-identical to the primary's post-commit file* (log
  shipping through the pager; replay idempotent). After divergence the
  ends are not identical and the ship is refused — divergence is visible
  and typed, never merged.
- **Heal semantics:** primary is authoritative. A divergent replica's
  local commits are reported (count and seq range) by the typed error
  before resolution discards them — losing divergent replica-local
  writes is allowed by policy; *silent* loss is not (M27's rule).
- **Divergence stays API-reachable** (replica-local writes permitted in
  the partitioned state), so the refusal invariant is testable rather
  than unreachable by construction.
- Replication lag never yields a torn logical row (all-or-nothing per
  transaction); lag is a declared readout, not an inference.
- Deterministic tests drive the replica through scripted partitions
  (seeded, replayable — M24's seed discipline), not timing races. The
  replica apply path inherits M24's crash sweep with its site-coverage
  table (`died_here` per site) — coverage, not just a pass count.

**M36 acceptance invariants (priced, six):** (1) convergence without
partition — replica byte-identical after N commits, corpus run against
the replica file, not spot selects; (2) batch atomicity — kill inside
apply leaves pre- or post-state, never mixed, applied through the pager
so M24's crash sites apply unchanged; (3) replay idempotence — applying
the same batch twice leaves the file unchanged; (4) divergence refused —
typed error naming the base seq, both files byte-unchanged (sha
before/after), no committed row lost (the milestone's negative control);
(5) no silent loss after any heal — checked by row comparison, never by
`integrity_check`; (6) lag observable — replica answers from its own
committed state while partitioned, lag a declared readout.

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
