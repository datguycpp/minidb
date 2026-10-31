# Tier-5 prework (owner: Research) — M29–M33 acceptance definitions

Drafted before Tier 5 approaches. Several Tier-5 items look differential
and are not; fixing that here prevents a wrong harness at the boundary.

## M32 — EXPLAIN: NOT a conformance surface (ruling)

SQLite's `EXPLAIN` returns **bytecode for SQLite's VM**; MiniDB has a
different executor, so bytecode rows can never match and differential
comparison of `EXPLAIN` output is meaningless by construction.
`EXPLAIN QUERY PLAN` lines (`SEARCH t USING INDEX …`) are SQLite's
vocabulary describing SQLite's planner — likewise not byte-comparable.

- What M33-era testing does instead: `EXPLAIN` output is checked against
  **our own** documented plan vocabulary (shape/golden tests in our
  suite), while plan *correctness* stays with `tools/plan_check.py`
  (chosen access path, rows examined, result identical) — the evidence
  pattern already used at M4/M5.
- **Corpus rule:** no corpus case may put `EXPLAIN` rows through the
  differential harness. If a future tranche wants plan assertions in the
  corpus, the mechanism is a harness directive that asserts *properties*
  (e.g. "uses index"), never raw plan rows — and it needs a spec edit
  first (directives roadmap).
- The riders from M4/M5 ("score can't say index works") close at M32 only
  in the *human-readable* sense: plans become inspectable; the evidence
  remains plan_check.

## M33 — docs examples verified by running it (harness, spec'd now)

- Format contract: runnable examples live in fenced blocks tagged
  `sql` with a one-line header comment naming the fixture
  (`-- fixture: empty | demo`), and expected-output lines when the doc
  shows results (`-- expect: …`).
- `tools/docs_check.py` extracts every runnable block, executes it
  against a fresh database (pinned reference for expected outputs,
  MiniDB under test for acceptance), and fails on: SQL that errors,
  expected output that differs, or a `sql` block with no fixture header
  (missing metadata is a failure, not a skip — the t1b inert-directive
  lesson applied to docs).
- Acceptance: **zero failures, blocks checked = blocks found, both counts
  printed.** A docs change runs docs_check before commit — same
  standalone-run protocol the red-push incidents bought for tests.
- Every Tier-5+ doc (CLI help text, binding README, protocol notes)
  inherits the contract; examples that cannot be verified must say
  `-- example: illustrative` and are excluded by declaration, not by
  silently dodging the runner.

## M29 — CLI shell: declared subset, not sqlite3 parity

- `.tables`, `.schema [name]`, `.quit`, `.help` are the gate set;
  everything else from sqlite3's dot-command zoo is out of scope until
  declared. `import`/`export` CSV is in (ladder says so): acceptance is
  round-trip — CSV → table → CSV equals, file provenance in the report.
- Ruling: the CLI is *our* shell speaking SQL we already conform on;
  conformance is the SQL, not the dot-commands (SQLite's dot-command set
  is not a standard to match).

## M30 — network server: own protocol, stated plainly

- **SQLite has no wire protocol to be compatible with** — there is
  nothing differential to run. Acceptance is functional: N concurrent
  clients, interleaved queries, results identical to local in-process
  answers for the same statements (differential against the *engine
  locally*, which is real), and — IN-WRITING REPLACEMENT for the original
  connection-isolation line, made after M30's probe showed it
  unreachable-by-design under the shared-handle lock model: *while client
  A holds an open transaction, B's statements wait and then fail with the
  reference wording; after A commits, B sees the committed state.*
  (Measured at the server layer as of M30's fix. Added as a sixth
  invariant: a client disconnecting mid-transaction rolls back, so a dead
  socket cannot leave the database locked — the original line never
  imagined it, and unreachable invariants get replaced in writing rather
  than dropped, per the M25 precedent.)
- Concurrency semantics are defined in the Tier-4 work (M25): the server
  inherits them; it does not invent them.

## M31 — bindings (Node or Python client)

- Acceptance: one binding, README example verified by the M33 docs
  runner, round-trip test (connect → schema → insert → query → compare
  with local), and error propagation (server-side SQL error arrives at
  the client as a typed error, message text identical to local run).
  Lock-model consequence, added after M31: the local comparison uses an
  **in-memory** database because the server holds M25's exclusive file
  lock (a second handle on the same path is `database is locked`), so the
  parity check must be paired with a **post-shutdown persistence check** —
  after `server_close()`, open the file directly: proves the lock was
  released AND the rows reached disk, in one assertion.

## Order of authoring

M29–M33 specs needed before their builds start, in ladder order. When
Tier 5 approaches, this doc gets a delivery confirmation like every
tranche; predictions for milestone outcomes get registered then, when the
engine's state is measurable — not here, where they'd be noise.
