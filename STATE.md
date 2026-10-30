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

- **Current milestone: M9 accepted (fuzzer + spec delivered); Tier-1 gate verification
  pending — requires t1c AND t1e in the denominator first (see decision log). M10 next.**
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
  - `SCORE t1.5 (M5): 41/43 (95.3%) [Δ +0 — expected; the corpus cannot observe plan
    choice]` — plan evidence instead: `tools/plan_check.py` extended to five scenarios
    (Default re-ran: all PASS). **Default's edge probes of the documented rule
    (docs/planner.md): equal-estimate tie-break → name order (`aaa` over `zzz`);
    unique-preference disambiguated from name order (UNIQUE `zzz` beats regular `aaa`)
    — confirmed; bucket == table size → full scan with `reason=index-not-selective`;
    range predicate → full with `no-eligible-index`. `last_plan` carries reason,
    estimated_rows and per-candidate estimates — the choice is auditable post-hoc.
    142 unit tests OK, syntax 46/46, durability PASS.**
  - **`SCORE t1.5+ (t1b landing, re-baselined #3): 50/52 (96.2%) [Δ +9 cases, +9 passes,
    machinery added]`** — old score on old corpus: 41/43 (95.3%) at 1c24177; new score on
    new corpus: **50/52 (96.2%) at d6d0ac6**. Cases added: 9 (five index-outcome — M4/M5
    territory, all pass first run; four reopen — persistence now SCORED for the first
    time: rows visible after reopen, append-after-reopen persists, catalog survives,
    constraints survive post-reopen via error agreement). **Research's pre-registered
    prediction (50/52, 96.2%, posted before the run) confirmed exactly.** The four
    previously-inert reopen cases are now the mechanism's own proof of work: under the
    old loader they passed without testing anything; under the new one they execute the
    reopen cycle and can fail (negative controls prove it).
  - `SCORE t1.6 (M6): 50/52 (96.2%) [Δ +0 — expected; transaction cases land with t1d]` —
    differential evidence in `tools/transaction_check.py`: 8/8 shapes match SQLite
    (Default re-ran from cold clone). **Default's independent probes against the pinned
    binary: all three error wordings byte-exact (`cannot commit - no transaction is
    active`, `cannot rollback - no transaction is active`, `cannot start a transaction
    within a transaction`); DDL rollback verified on the binary (post-ROLLBACK SELECT →
    `no such table: ghost`, matching MiniDB); close-with-open-transaction rolls back on
    BOTH engines (binary: BEGIN+INSERT, no COMMIT, fresh invocation sees nothing;
    MiniDB: `[]` after reopen). Durability 3/3 incl. rolled-back row absent. Snapshot-
    per-transaction is O(database size) — documented cost, replaced by M7's WAL. 174
    engine tests + harness suite = 176 OK, syntax 46/46.** The snapshot-per-transaction
    cost is formally superseded by M7's WAL — transaction machinery unchanged otherwise.
  - `SCORE t1.7 (M7): 50/52 (96.2%) [Δ +0 — the corpus cannot observe crash recovery]` —
    evidence instead, all reproduced by Default from a cold clone: `tools/crash_check.py`
    4/4 (real `os._exit(9)` children at a fault seam on both sides of the fsync; open
    transaction lost, committed row kept; ten consecutive crashes). **Default's
    additional sweep — crash at every commit position via `MINIDB_CRASH_COMMIT` 1–5,
    fresh file, CREATE+INSERT child:** commit1 (header batch) → valid empty db (`no
    such table`, correct — nothing durable yet); commit2 (CREATE batch) → table exists,
    zero rows (correct — INSERT not durable); **commit3 (the INSERT's own commit) →
    row (42, 'committed') survives via WAL replay from a 0-byte main file** (the bug-#1
    shape); commits 4–5 → idempotent reopen. The three reported bugs' fixes confirmed in
    the code: recovery runs before the new-file decision, post-ROLLBACK catalog_root
    handled, `close()` flushes instead of silently dropping dirty pages. WAL format read
    line-by-line in pager.py: per-frame CRC + batch commit-marker CRC +
    torn-tail/partial-frame/CRC-mismatch → truncate-and-discard at every exit. Rollback
    O(1) confirmed in code (buffer dropped, no file copy). **New limits recorded with
    owners:** redo log ≠ SQLite's WAL (single writer, no readers during recovery,
    whole-batch apply) — M18/M24. Timing re-measure owed by the M7 report (non-blocking;
    carried). 185 engine tests OK, syntax 46/46, behaviour 50/52, all tools green.**
  - **`SCORE t1.7+ (t1d landing, re-baselined #4): 61/63 (96.8%) [Δ +11 cases, +11 passes,
    harness fix]`** — old score on old corpus: 50/52 (96.2%) at d6d0ac6; new score on new
    corpus: 61/63 (96.8%) at 4e29319. Cases added: 11 (transaction shapes from M6's
    evidence + DDL-index rollback + two reopen combinations — all pass). **Research's
    pre-registered prediction (61/63, posted before the run) confirmed exactly — 2 for 2
    on falsifiable predictions.** The landing also caught a REAL harness defect
    immediately: Default's t1b runner ran MiniDB statements through `db.execute()` instead
    of `db.run()`, defeating the engine's autocommit rule — a phase ending in ROLLBACK
    discarded pre-transaction statements (the CREATE never committed) while SQLite's side
    committed them: different transaction semantics per side on the same case. **The
    engine was right; the harness was wrong.** Landed raw at 60/63 with that FAIL visible
    (t1d case 11: `SQLite returned rows, MiniDB errored: 'no such table: t'`), then fixed
    (`db.run` everywhere). Second Default-authored harness defect caught by a tranche's
    first run.
   - `SCORE t1.9 (M9): fuzz pipeline delivered; gate metric measured raw — see decision
    log` — **the Tier-1 conformance score is unchanged (61/63, 96.8%); this milestone
    measures a different quantity.** Engineer's published gate run: 25/10k
    (22 rejection-position + 2 float + 1 FROM-less). **Default's independent re-run on
    the same seed AFTER the two pre-filing fixes: 7/10k, all float-last-digit**
    (`avg(v)` differing in the 15th significant digit only) — the rejection-position
    class was eliminated by the fixes, confirming both the defect reading and the float
    ruling's necessity. The published gate number was pre-fix; post-fix is the honest
    current rate.
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
- Tier-1 budget: **18 rounds after Steven's +6 top-up. Used: 18** (M7 build 17, M8
  build+review 18). Budget rule changed by
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

1. [Build] M8 — joins, aggregates, subqueries (ordered, next). Acceptance: corpus
   differential (research's t1c tranche is authored against THIS milestone); raw output
   on every new statement shape; the plan-check scenarios extended to at least one join
   access path (M5's rule documents eligibility for tables — a join must not silently
   full-scan-×-full-scan without the planner saying what it did).
2. [Build] M9 — fuzzing against SQLite, after M8. Research's fuzzer spec + t1e seeds land
   here. **Tier-1 gate is formally verified after M9** — the arithmetic (96.2% ≥ 90%) is
   already cleared, but the tier closes only when milestones 1–9 are done AND the full
   tranche set is in the denominator (decision log, Tier-1 gate timing).
3. [Research + Default] **t1d (11 pre-drafted cases) + the two loader fixes land NEXT** —
   one re-baseline event, before the M8 build: the transaction shapes all pass on M6
   evidence, so landing them now moves the denominator to 63 at no risk, and the
   tranche plan has t1d owned by M7, which is complete.

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
  → **CLOSED by M7** (WAL with CRC frames, crash sweep 5/5 commit positions correct).
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
- 2026-10-30 (M5): M5 **accepted** — cold-clone reproduction (142 tests OK, syntax 46/46,
  behaviour 41/43, durability PASS, plan_check 5/5). Rule verified at its edges by
  Default: name tie-break, unique preference (disambiguated from name order by probing
  UNIQUE-named-zzz vs regular-named-aaa), non-selective fallback (bucket == table →
  scan), range/OR exclusion. Planner rule documented in docs/planner.md — "the planner
  picked it" is no longer an unexplained answer.
- 2026-10-30 (t1b landing — re-baseline event #3, executed by Default per the standing
  directive): runner machinery implemented — `--directive: reopen` is POSITIONAL
  (phases split at markers; multi-reopen cases supported; SQLite side = one CLI
  invocation per phase on a temp file, MiniDB side = Database(path)/close/reopen),
  `--directive: sort` implemented case-level (sort by JSON repr — no inert members in
  the known set), loader errors on unknown marker values and near-miss keys
  (edit distance ≤ 1: `--reopn:`), **prose `-- reopen: ...` lines stay comments** (the
  marker is the only directive syntax; corpus prose predates it — reconciled reading of
  the error-on-unknown rule, which cannot mean failing on the draft's own case
  descriptions). Negative control: 12 new harness tests, including breaking reopen on
  EACH side independently and asserting the constraints-survive case FAILS.
  Default's own landing bug found and fixed before commit: the first test asserted
  surviving rows via a case whose honest outcome is ERROR AGREEMENT (the violating
  insert aborts the phase before any SELECT) — the runner was right, the test's
  expectation was wrong; fixed by testing both shapes (rows-visible and
  error-agreement). Corpus: t1b 9/9 first run, 50/52 (96.2%), research's prediction
  exact. Build reviews the full runner diff next round.
- 2026-10-30 (M6): M6 **accepted** — cold-clone reproduction (transaction_check 8/8,
  durability 3/3 incl. rolled-back-row-absent, syntax 46/46, behaviour 50/52). Default's
  probes against the pinned binary all exact (wordings, DDL rollback, close-rolls-back on
  both engines). Snapshot-per-transaction cost ratified as documented-and-replaced-by-M7.
- 2026-10-30 (harness fixes from Build's M6 review): both defects confirmed and fixed by
  Default (dead `pending` state removed; empty/comment-only corpus file now raises a
  clean error with two new tests). **Process failure logged honestly: the first fix
  commit (a9e6d6b) shipped with a failing test** — Default chained `unittest | tail` and
  `git commit` in one shell line, and the pipe masked the exit code, so the failure
  scrolled past. Caught on the next command, fixed in `4c4199a` (the guard had counted
  ANY comment as a directive marker; corrected to directive markers only), invariant
  asserts added in `9308de2`. Lesson: never chain a commit after piped test output —
  pipefail or separate turns.
- 2026-10-30 (M7): M7 **accepted** — cold-clone reproduction (185 tests, syntax 46/46,
  behaviour 50/52, crash_check 4/4). **Default's own probe initially produced a false
  alarm** (first-commit crash → `no such table`) — the child had crashed inside the
  header-initialisation commit, before CREATE was durable, so `no such table` was correct
  behaviour, not data loss; logged rather than hidden. The subsequent sweep (crash at
  every commit position, 1–5) verified recovery correctness at each, including the
  headline case: kill during the INSERT's own commit, 0-byte main file + valid WAL → row
  survives. M6's cost estimate (snapshot-per-transaction) formally superseded — rollback
  is now O(1). Timing re-measure still owed by the M7 report (acceptance asked for it;
  nothing arrived in the milestone report — non-blocking, carried as a review note).
- 2026-10-30 (Tier-1 gate timing, precise): engineer's arithmetic is correct — 50/52 =
  96.2% ≥ 90% — but the charter defines Tier 1 as milestones 1–9, so the gate is the
  tier-COMPLETION gate, formally verified after M9 with the full tranche set (t1b/t1c/
  t1d/t1e) in the denominator. Recording this now so nobody closes Tier 1 early on a
  partial denominator.
- 2026-10-30 (t1d landing — re-baseline event #4): executed by Default after engineer
  advanced to M8 build before the ordered landing — sequencing deviation logged; the
  re-baseline stays attributable to one tranche. **Landed raw at 60/63 with ONE FAIL**
  (t1d case 11: `SQLite returned rows, MiniDB errored: 'no such table: t'`), investigated
  live: manual probe through `db.run()` passed, runner probe through `db.execute()`
  failed the same shape → root cause: the harness' MiniDB side never autocommitted, so a
  phase ending in ROLLBACK discarded pre-transaction statements (the CREATE never
  committed) while SQLite's side committed them. **Engine semantics correct throughout —
  the CLI-autocommit parity is the harness's to carry.** Fixed (`db.run` in the runner;
  second Default-authored harness defect caught by a tranche's first run), suite re-run:
  **61/63 (96.8%), research's pre-registered prediction exact.** The two loader fixes
  engineer proposed at the M6 review were verified already at HEAD. Budget cap consumed
  (18/18) but suspended per the standing directive.
- 2026-10-30 (M8): M8 **accepted** — cold-clone reproduction (204 tests, syntax 46/46,
  behaviour 50/52 at the pre-t1d denominator, plan_check join scenario: nested-loop,
  examined=40000 = scan×scan stated honestly, with a test now guarding that the reported
  plan is the executed one). Default probes vs the pinned binary: GROUP BY ordering with
  NULLs-first exact-match; empty-table aggregates exact (0 and four NULLs). **Two scope
  findings: (a) `NOT IN (SELECT …)` is a parse-level gap (`near "NOT": syntax error`) —
  research's t1c swing case settles at 68/75, not 69; (b) `SELECT t.a` (qualified
  reference, single table, no alias) is an undeclared gap (`no such column: t.a`) —
  ordered INTO M10's scope (cheap parser fix, needed by any future multi-table work).**
  INNER-only join scope ruled acceptable as stated in the report (ladder wording
  unqualified; OUTER/comma-join remain owned failures, tracked for the gate decision).
- 2026-10-30 (early scope landing, Default-ratified): engineer fixed the two M8 review
  findings in `3579571` as two isolated additive commits — ahead of M10's position in the
  ladder. Default verification: cold-clone, 208 tests OK, syntax 46/46, corpus unchanged
  at 61/63 (4e29319); probes vs the pinned binary exact (NOT IN with NULL in the set →
  empty result on both engines; IN → [(1,),(2,)]; `SELECT u.a` → header `a`, value 2).
  **Ruling: the early landing is accepted** — engineer's argument stands ("IN without NOT
  IN is half of a predicate"), the landings are isolated and reversible, and the
  NULL-trap differential test is now durable rather than assumption. **Consequence:
  research's t1c prediction settles at 69/75** (the swing case passes; only the five
  join-scope failures + two M10 cases fail). STATE's "owned failures" adjusted
  accordingly. `execute(autocommit=False)` keyword ratified for M10's scope (the
  call-site distinction kills the two-spellings-of-query hazard before Tier 2).
- 2026-10-30 (gate-margin ruling — Default's answer to Research's flag before M9): the
  Tier-1 gate is verified on the COMPLETE t1a–e denominator at M9 completion — that is
  not revisitable, and no scope expansion happens to protect the number. Margin flag
  accepted and will be reported at every landing between here and the gate: post-t1c
  predicted ~68/75 = 90.7% (0.7 points of headroom), and t1e's seeds can move it either
  way. **If the verified number lands below 90%, Tier-1 gate verification reports NOT
  MET and the remedy options (e.g. a focused LEFT/OUTER-join milestone vs accepting the
  miss) go to Steven — no corpus edits, no silent re-scoping.** Current owned failures
  mapped to that decision: NOT IN (M10), the five join-scope cases (declared at M8),
  FROM-subquery (M10-adjacent, owned), plus whatever t1e adds.
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
- 2026-10-30 (M9): M9 **accepted** — cold-clone reproduction (210 tests, syntax 46/46,
  behaviour 61/63, corpus revision 4e29319; fuzz.py re-run by Default: seed 3, 10k
  queries → **7 mismatches, all float-last-digit**). Fuzzer implements the spec:
  structured/mutation/rejection generators, seed-logged, comparison through the harness
  normalizer only (no second comparator), minimization on every hit, MiniDB tracebacks
  counted as mismatches regardless of SQLite's answer. **Engineer's published 25/10k was
  pre-fix** — the two fidelity fixes it found (reserved-word split for identifiers
  vs keywords; no trailing semicolon on the reference script) belonged to the rejection
  class; Default's post-fix re-run shows that class gone on the same seed, leaving the
  float class — which is exactly the input the float ruling needed.
- 2026-10-30 (M9 ratifications — both rulings verified by Default's own post-fix data,
  one flagged to Steven per the standing rule): (1) **Tier-2 gate metric becomes the
  semantic rate** (dialect-valid mismatches); rejection-position rate stays printed raw
  forever and monotonic non-increasing between gates; edit-distance-of-valid-syntax
  repros become ordinary corpus cases, pure soup stays diagnostic. **FLAGGED: Steven, a
  charter requirement ("<1 fuzzer mismatch per 10k" unqualified) is unreachable as
  literally worded (22/25 were error-token placement on random soup — no user value) and
  this redefines the metric BEFORE Tier 2 rather than working around it silently; one
  word rejects the split and restores the literal gate.** (2) **Float comparison: 1e-12
  relative tolerance, COUNTED per comparison and reported raw every run** — the
  reference's 15-sig-digit JSON loses digits (SQLite's serialization, not a MiniDB bug:
  exact-after-round-trip was unsatisfiable against the tool framing the normalizer
  exists to strip). Counts stop the tolerance from becoming an unmonitored softer
  failure mode. **Zero-delta re-baseline entry required for the normalizer amendment**
  (any normalizer change → entry — see below).
- 2026-10-30 (re-baseline event #5 — zero delta): the spec's "exact equality after
  round-trip" normalizer replaced by the counted 1e-12 relative tolerance **before**
  any scored re-run against new floats: corpus floats (1.5, 2) are untouched, verified
  corpora identical, scores unchanged → Δ0 recorded here. Counts reported with every
  fuzz line henceforth.
- 2026-10-30 (Tier-1 gate correction — responding to Build's "the Tier-1 gate
  verification is yours and the number is 61/63"): 61/63 (96.8%) is the CURRENT
  denominator, not the gate number. Per the recorded ruling (and twice already), the
  gate is verified with t1a–e in the denominator — that requires t1c (+12, predicted
  69/75 = 92.0%) and t1e (research-owned seed tranche due at this milestone) to land
  first. Margin status going in: 2.0 points of headroom at 12/13 milestones; **the
  verified gate report follows those two landings, and the 90% line decides it.**
