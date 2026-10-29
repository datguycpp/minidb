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

- **Current milestone: M5 — query planner (index vs full scan). CHECKPOINT 1 follow-on
  approved: Steven's 2026-10-30 directive "finish up all of the milestones, i approve of
  both of them" = budget topped up +6 AND a standing instruction to run the ladder to the
  end of Tier 6 without pausing at gates (see decision log for the exact interpretation
  and Steven's exit clause).**
- Scores (all cold-clone-reproduced by Default):
  - `SCORE t1.1: syntax 46/46 (100.0%) [Δ baseline — first measurement]`
  - `SCORE t1.2: behaviour 21/21 (100.0%) [Δ +21 vs M1]`, `syntax 46/46 (100.0%) [Δ +0]`
  - `SCORE t1.2 (re-baselined): 35/41 (85.4%) [Δ −6 vs pre-corpus 21/21 behaviour; +20
    cases]` — corpus `behavior.txt (21) + t1a.txt (20)`, corpus revision f12cee8
  - `SCORE t1.2.5: 39/41 (95.1%) [Δ +4 vs re-baselined 35/41]` — syntax unregressed
    (46/46), 94 unit tests OK, corpus revision f12cee8, reference SQLite 3.54.0.
  - **`SCORE t1.2.5 (re-baselined #2): 41/43 (95.3%) [Δ +2 cases, engine unchanged]`** —
    old score on old corpus: 39/41 (95.1%) at corpus revision f12cee8; new score on new
    corpus: 41/43 (95.3%) at 1c24177. Cases added: 2 (bracket/backtick quote-style).
    **Engine unchanged, no code diff — the +0.2pp is pure corpus growth, not progress.**
  - `SCORE t1.3 (M3): 41/43 (95.3%) [Δ +0 — expected; the corpus cannot observe
    persistence]` — durability is evidenced separately, not scored: `tools/
    durability_check.py` (Default re-ran from cold clone: write/reopen sha256 identical,
    append-after-reopen persisted; plus Default's own probe — CHECK constraint enforced
    identically after reopen). Syntax 46/46, 114 unit tests OK, corpus revision 1c24177
    (engine-only commits, correctly unchanged).
    - `SCORE t1.4 (M4): 41/43 (95.3%) [Δ +0 — expected; the corpus cannot observe index
      choice]` — plan-level evidence instead: `tools/plan_check.py` (Default re-ran from
      cold clone: full-scan 2000 examined before, index access 1 examined after, index
      chosen for equality). **Default's additional plan probes via `last_plan`
      introspection: prefix-only on a composite index → full (no partial-index results),
      both columns constrained → index, OR predicate → full, single-column equality →
      index. Message conformance against the pinned binary: composite violation =
      `UNIQUE constraint failed: t.a, t.b` (exact), create-unique-over-duplicates =
      `UNIQUE constraint failed: t.id` (exact), two NULLs coexist under UNIQUE (exact),
      failed create leaves no index registered (dup insert succeeds afterwards — Default
      probe). 133 unit tests, durability PASS, syntax 46/46.**
  The pre-corpus 100% measured a corpus that hadn't asked the hard questions yet; the
  honest trajectory is 35/41 → 39/41 and Tier-1's gate is 90% of the conformance suite.
- Tier-1 budget: **18 rounds after Steven's +6 top-up. Used: 12.** Budget rule changed by
  Steven's standing directive ("finish up all of the milestones"): the stop-and-ask cap is
  **suspended for the rest of the project** — round counts are still logged here for
  transparency, and Default still flags anything impossible or too slow per the charter's
  honesty rule, but the ladder no longer pauses at a round count. Checkpoint cadence
  stays (every 3 milestones) but checkpoints are **reports, not approval waits** —
  advance approval stands for any checkpoint whose verification passes.
- Language: **Python, stdlib-first** — confirmed by Build at M1. Revisited at the Tier-3
  gate as planned (hot-path rewrite is M38).
- Reference caveat: box Python `sqlite3` module is 3.53.1; the harness deliberately shells
  out to the `sqlite3` CLI (3.54.0 = pinned). The pin lives in `tools/syntax_conformance.py`
  (`PINNED = "3.54.0"`) **and** here — change both together; the tool prints `*** PINNED IS
  ***` when the CLI drifts from the pin.

## Next three tasks

1. [Build] M5 — query planner (index vs full scan). **Ordered, started.** Scope: a real
   choice layer over M4's eligibility — when multiple indexes are eligible, choose one
   by an explicit rule (documented, not vibes); when none, full scan; `last_plan`
   enriched accordingly. Acceptance: `tools/plan_check.py` extended to cover choice
   scenarios (multi-candidate, no-candidate) with raw output; corpus re-run raw;
   syntax unregressed; the five index-outcome cases in Research's parked t1b draft
   should pass when it lands post-M5 (they are NOT in the denominator yet).
2. [Research] t1b + `--directive: reopen` landing the round after M5 — one re-baseline
   event, negative control included, per the ratified spec. Then t1c/t1d/t1e per
   tranche plan; Tier-2 suites drafted as milestones complete so gates are measured
   against suites written before the fixes.
3. [Build] M6 — transactions, after M5 lands with numbers.

## Known failures

- `SELECT 1` (no FROM) is valid SQLite; our parser rejects it — in Research's t1a corpus
  (cases 4–5, now erroring `incomplete input` in SQLite's wording); **expected to fail**
  until implemented. M10.
- No type affinity — `'5'` into an INTEGER column stores text. M17.
- Arithmetic in expressions (`a + 1 > 2`, `id + 1`) unparsed. M10.
- No ORDER BY / LIMIT / OFFSET / DISTINCT / GROUP BY / HAVING. M12.
- No DELETE / UPDATE / DROP; table-level `CHECK` rejected. M16.
- No joins or aggregates. M8.
- Column DEFAULT evaluated without SQLite's constant-expression rules — untested against
  quirks, flagged rather than assumed correct.
- No crash safety: writes are write-through, fsync only on clean close — M7 (WAL) owns.
- Rows larger than a page raise `row too large` — M20 (overflow pages) owns.

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
- 2026-10-30 (M2.5): M2.5 **accepted** — cold-clone reproduction (94 tests OK, syntax
  46/46, behaviour 39/41). Default independently confirmed against the pinned binary:
  `SELECT 1e` → `unrecognized token: "1e"` (crash fixed), unterminated block comment
  accepted as comment-to-EOI (fix real; `SELECT 1 /*…` still fails only via the known
  FROM gap), bracket/backtick identifiers → `no such column: apple` without the hint,
  and ParseError.detail preserved alongside conformance wording. Build's nit on the
  5e62a3c commit message was correct — the diff carried spec + runner only, not t1a
  (Build's f12cee8 already had it); logged as a message-accuracy lesson, not corrected
  retroactively.
- 2026-10-30 (re-baseline event #2): t1a grew 20 → 22 cases (quote-style identifiers).
  Old score 39/41 (95.1%) at corpus f12cee8; new 41/43 (95.3%) at 1c24177. **Cases added,
  engine unchanged** — a score rise with zero code diff is corpus growth, recorded here
  so no reader mistakes it for progress. Both new cases pass on first run (Build had
  implemented the quote-style distinction at M2.5); the `-dirty` corpus-revision suffix
  was observed live on the uncommitted edit before commit — mechanism validated in the
  field, not just in tests.
- 2026-10-30 (CHECKPOINT 1): three milestones complete (M1 parser, M2 in-memory tables,
  M2.5 error wording). Score trajectory 35/41 → 39/41 (95.1%) behaviour, 46/46 syntax.
  Remaining known failures: 2 corpus cases (SELECT-without-FROM, M10) + features not yet
  reached (M8 joins, M10 expressions, M12 ordering, M16 constraints/DML, M17 affinity).
  Budget: 7 of 12 rounds. M3 (disk storage, B-tree) ordered on approval.
- 2026-10-30 (CHECKPOINT 1 approved): Steven approved ("just keep going"). M3 opened;
  round 1 = on-disk page-format decision (expensive to reverse). Research's spec
  amendments ratified as-is: `--sort`/`--reopen` directives roadmap (reopen lands with
  t1b, not mid-M3), crash-recovery and index-choice evidence is engineer-owned because
  the corpus cannot observe them (M4 rider: plan-level artifact required for any "index
  works" claim). No re-baseline — no normalization or scoring change in the spec edit.
- 2026-10-30 (budget warning, threshold 9): Tier-1 budget at 9 of 12 rounds. At the
  current pace (M3 ≈ 2 rounds, M4 ≈ 1) the cap lands during M4's review. Default will
  stop and ask Steven to top up the budget before spending round 12.
- 2026-10-30 (M3): M3 **accepted** — cold-clone reproduction (114 tests OK, syntax 46/46,
  behaviour 41/43, corpus revision unchanged at 1c24177 for engine-only commits).
  Default re-ran the durability tool: write/reopen sha256 identical, append persisted;
  **Default's additional probe: CHECK constraint still enforced after reopen** (violating
  insert fails with SQLite's wording), independently confirming the store-original-CREATE
  catalog design. Runner zero-diff claim verified (`git diff` across both commits empty).
  `Database(path)` signature change reported separately as required. Format doc read and
  ratified: page-0 header, B+tree with leftmost-pointer interior nodes, deliberately-
  absent table maps every gap to its owning milestone. M3 counted as ONE round per the
  new counting rule (milestone = one round regardless of commit count).
- 2026-10-30 (M4): M4 **accepted** — cold-clone reproduction (133 tests OK, syntax 46/46,
  behaviour 41/43, durability PASS). Every message claim probed against the pinned
  binary (composite UNIQUE wording, NULL exemption, create-over-dup error text — all
  exact) and the failed-create-leaves-no-index behavior probed on the engine (dup insert
  succeeds after the failed create). Plan eligibility claims verified via `last_plan`:
  prefix fallback, full-composite use, OR fallback, single-column use — all as stated.
  Index definitions persist / structures rebuild ratified (a second structure that can
  disagree with data pre-WAL is worse than a slow scan — documented in the format doc).
  M4 closes the M3 uniqueness-scan limit. **Directive-syntax amendment ratified for the
  t1b landing: distinct marker + error on unknown directive-shaped lines** (Build's
  amendment to Research's negative-control requirement). Round 12 consumed; ladder
  paused pending top-up.
- 2026-10-30 (standing directive — the entry later tiers assume): Steven said
  **"finish up all of the milestones, i approve of both of them."** Default's recorded
  interpretation: (1) the +6 top-up is approved; (2) "both of them" and "finish up all"
  together read as **advance approval to run the entire ladder (Tiers 1–6) without
  pausing for per-checkpoint or per-tier approval**, provided each gate is met and
  verified. Tier gates (90% T1, 95% T2, etc.) become **verification events Default
  reports**, not stopping points; the every-3-milestones checkpoint cadence stays as
  reporting. **Exit clause preserved:** if Steven meant only Tier 1, one word at any
  checkpoint restores the pause rule from that point. Rounds are logged, not enforced.
  Irreversible actions still require explicit words per standing rules.
- 2026-10-30 (t1b draft verification): Default ran Research's parked `docs/t1b-draft.txt`
  (9 cases, outside the runner glob — parking confirmed: main run still 41/43) through the
  CURRENT runner: **4/9**. The five index cases fail correctly per decision 2
  (`near "UNIQUE": syntax error` etc. — no CREATE INDEX until M4). **The four `--reopen`
  cases PASS silently** — the current loader treats `--` lines as comments, so the
  directive is inert and the cases run as plain sequential statements against in-memory
  engines. Research's parking rationale ("would fail for the wrong reason") is therefore
  **empirically backwards: accidental early inclusion would inflate the score with fake
  passes, not fail loudly** — the parking is right, but for the opposite reason.
  **Ratified requirement for the `--reopen` implementation (M5+, Research-owned spec
  wording): the directive must ship with a negative control — a harness test that breaks
  the reopen step and asserts the constraints-survive case FAILS — or four corpus cases
  can silently no-op forever.**
