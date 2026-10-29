# MiniDB — STATE

A SQL database engine built as a conformance project, scored against **real SQLite**.
Governance, ladder, decisions, and score live here. Updated after every milestone.

**Score line format — every milestone report must include one:**

```
SCORE <tier>.<n>: <passed>/<total> (<pct>%) [Δ <sign><change> vs <last reported milestone>]
```

## Pinned reference

- **SQLite 3.54.0** (2026-10-09, the machine's `sqlite3`). All conformance numbers are
  against this version and only this version.
- If the reference version ever changes, this file records: old version, old score, new
  version, re-baselined score — in one entry. Scores across versions are not comparable.

## Ratified decisions (2026-10-30)

1. **Reference behavior is pinned SQLite, quirks included** (`1 IN ()`, dynamic typing,
   and friends). Our opinions about SQL correctness are irrelevant to the score.
2. **Not-yet-implemented = failed, always.** A test leaves the report only as a logged
   *known divergence* (a consciously accepted behavioral divergence from SQLite, named
   here). Excluded tests stay in the denominator. Skips that hide missing features are
   rejected in review.
3. **Result normalization is harness-owned.** Row ordering, float formatting, NULL
   rendering — normalized identically on both sides, in a file owned by Research. Build
   never edits it. *Default amendment:* any normalization change must re-run the corpus
   and report the before/after score delta in the same entry, so historical scores stay
   comparable when the normalizer moves.
4. **Raw test output in reports**, not summaries. Failures are reported as failures,
   never quietly patched and passed.

## Governance

- Roles: **Research** — specs, corpus, fuzzer, benchmarks; never engine code.
  **Build** — one milestone at a time, runs tests after each; never changes scope.
  **Default** — plans, backlog, ordering, review, budget; never engine code.
- Tier order is fixed by the ladder. A tier starts only when the previous gate is met
  and Steven approved the checkpoint.
- Within a tier, Default orders milestones, preferring whichever unblocks the most
  failing tests.
- Checkpoint every 3 milestones: score line + failures + next steps, then wait for
  Steven's approval.
- Budget per tier, owned by Default; warn before exceeding, stop and ask at the cap.

## Ladder

**TIER 1 — Foundations** · gate: 90% of basic conformance suite
1. SQL subset parser (CREATE, INSERT, SELECT, WHERE)
2. In-memory tables
3. Disk storage with B-tree
4. Indexes
5. Query planner (index vs full scan)
6. Transactions
7. Write-ahead log and crash recovery
8. Joins, aggregates, subqueries
9. Fuzzing against SQLite

**TIER 2 — Real SQL coverage** · gate: 95% conformance, <1 fuzzer mismatch / 10k queries
10–17: expression engine; built-in functions; ORDER BY/LIMIT/OFFSET/DISTINCT/GROUP BY/HAVING;
UNION/INTERSECT/EXCEPT; views & CTEs (recursive); window functions; ALTER TABLE, constraints,
foreign keys, triggers; collations & type affinity

**TIER 3 — Storage engine depth** · gate: benchmark queries within agreed multiple of
SQLite's speed, identical output (Default sets the multiple after first measurement)
18–23: page cache with eviction; free-space management & vacuum; overflow pages; cost-based
planner with statistics (ANALYZE); covering/partial/multi-column indexes; open real SQLite
files (format compatibility)

**TIER 4 — Reliability and concurrency** · gate: 10,000 random crashes, zero data loss
24–28: crash torture; snapshot isolation (concurrent readers, one writer); locking &
deadlocks; per-page checksums & integrity check; property-based testing

**TIER 5 — Usability and ecosystem** · gate: docs examples all pass; clean-machine install
29–33: CLI shell; network server; language bindings; EXPLAIN; verified documentation

**TIER 6 — Stretch:** full-text search; JSON functions; replication; planner learning from
query history; hot-path rewrite in a faster language

## Current state

- **Current milestone: M2.5 — parser error-message conformance (Build; ordered before M3,
  it unblocks 4 of the 6 corpus failures). M3 — disk storage with B-tree — follows.**
- Scores (all cold-clone-reproduced by Default):
  - `SCORE t1.1: syntax 46/46 (100.0%) [Δ baseline — first measurement]`
  - `SCORE t1.2: behaviour 21/21 (100.0%) [Δ +21 vs M1]`, `syntax 46/46 (100.0%) [Δ +0]`
  - **`SCORE t1.2 (re-baselined): 35/41 (85.4%) [Δ −6 vs pre-corpus 21/21 behaviour; +20
    cases]`** — corpus `behavior.txt (21) + t1a.txt (20)`, corpus revision f12cee8,
    reference SQLite 3.54.0. Syntax suite unchanged at 46/46.
  The pre-corpus 100% measured a corpus that hadn't asked the hard questions yet; 85.4%
  is the honest baseline everything from here is measured against.
- Tier-1 budget: **12 team rounds** (a round = one message per role). Used: 5. **Warn at 9.**
  Checkpoint reviews don't count against the budget.
- Language: **Python, stdlib-first** — confirmed by Build at M1. Revisited at the Tier-3
  gate as planned (hot-path rewrite is M38).
- Reference caveat: box Python `sqlite3` module is 3.53.1; the harness deliberately shells
  out to the `sqlite3` CLI (3.54.0 = pinned). The pin lives in `tools/syntax_conformance.py`
  (`PINNED = "3.54.0"`) **and** here — change both together; the tool prints `*** PINNED IS
  ***` when the CLI drifts from the pin.

## Next three tasks

1. [Build] M2.5 — parser error-message conformance: ParseError text matches SQLite's
   byte-for-byte after framing strip (t1a cases 1–3, 6). Acceptance: corpus run 39/41+
   with raw output; no syntax-suite regressions.
2. [Build] M3 — disk storage with a B-tree, ordered, starts after M2.5 lands with numbers.
3. [Research] t1b tranche (storage/index observable behaviour) per spec lands after M5;
   no immediate deliverable until then.

## Known failures

- **Parser error messages differ from SQLite's** — 4 t1a cases fail on message text alone
  (cases 1–3 rejection text; case 6 loses SQLite's `no such column: "apple" - should this
  be a string literal in single-quotes?` wording; behavior matches in all four). **M2.5
  owns closing this.**
- `SELECT 1` (no FROM) is valid SQLite; our parser rejects it — in Research's t1a corpus
  (cases 4–5); **expected to fail** until implemented. M10.
- No type affinity — `'5'` into an INTEGER column stores text. M17.
- Arithmetic in expressions (`a + 1 > 2`, `id + 1`) unparsed. M10.
- No ORDER BY / LIMIT / OFFSET / DISTINCT / GROUP BY / HAVING. M12.
- No DELETE / UPDATE / DROP; table-level `CHECK` rejected. M16.
- No joins or aggregates. M8.
- Column DEFAULTs enforced but evaluated without SQLite's constant-expression rules —
  untested against quirks, flagged rather than assumed correct.

## Known divergences

- none yet

## Decision log

- 2026-10-30: Project chartered by Steven. Decisions 1–4 ratified by Default (with the
  normalization-delta amendment in #3). Tier-1 budget: 12 rounds. Reference pinned to
  SQLite 3.54.0. Milestone 1 ordered.
- 2026-10-30 (later): M1 **accepted** after Default's independent cold-clone review (57
  tests OK, score line reproduced, version guard and error-classification logic read and
  confirmed sound). Python stdlib-first confirmed by Build. Tier-3 gate principle: the
  speed multiple will be set from first measurement against what is honestly achievable
  for a Python engine — if the honest number makes the gate theatre, Default raises it
  with Steven instead of quietly loosening it.
- 2026-10-30 (M2): M2 **accepted** — cold-clone reproduction (88 OK, syntax 46/46,
  behaviour 21/21); CHECK-error wording divergence independently confirmed against the
  pinned binary (`CHECK constraint failed: n > 0`). Conformance-spec + t1a corpus delivered
  by Research (untracked); corpus wiring + combined run ordered as the re-baseline step.
  Case format: Build's differential design adopted as the single format per Research's
  spec; Research owns the spec file, Build never edits normalization rules (decision 3).
- 2026-10-30 (re-baseline event #1): corpus grew 21 → 41 cases (Research's t1a, +20).
  Old score: behaviour 21/21 (100.0%), syntax 46/46. New: **35/41 (85.4%) behaviour**,
  syntax 46/46 unchanged. Six failures: 4 parser-message-text (M2.5), 2 SELECT-without-FROM
  (M10). Wiring executed by Default under delegated judgment (Build's harness commits
  f12cee8/f189d1d already carried the glob and as-is file commits; Default added the
  spec-required corpus-revision output and ran the cold-clone verification). Build reviews
  the Default-written runner addition next round.
