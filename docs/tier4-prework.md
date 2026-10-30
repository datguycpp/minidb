# Tier-4 prework (owner: Research) — M24 crash torture + M28 property tests

Drafted before Tier 4 approaches (Tier 3 is at M19/M20). Same rule as
every other spec: authored before the milestone, so acceptance is executed
rather than negotiated.

## M24 — crash torture: 10,000 random crashes, zero data loss

**What the gate must mean (fixed here, before the runs):**

- A *crash* = a fresh process killed with `os._exit(9)` at a randomly
  chosen point inside commit/recovery code, via the existing
  `MINIDB_CRASH_POINT` seam extended to cover every fsync/write site
  (M7 proved the seam at two sides of fsync and five commit positions —
  M24 generalises the site list, not the mechanism).
- *Random* = seeded RNG picking (crash site × commit state × workload
  step), seed logged per iteration so any failing iteration replays from
  its log line alone. Reproducibility is acceptance, not polish.
- *Zero data loss* is checked after every reopen by **invariants**, not by
  "the file opened":
  1. every COMMITTED row from before the crash is present;
  2. no UNCOMMITTED row is present;
  3. every index agrees with its table (probe queries must return what a
     full scan says — a lying index is data loss's cousin);
  4. M27's `integrity_check`-equivalent passes when that milestone exists.
- **Rate is the gate:** 10,000/10,000 iterations clean. Any failure prints
  the seed, the crash site, the replay script, and the invariant that
  broke — one failing iteration is the milestone failing, not a flake to
  re-run.
- Report shape: `CRASH-TORTURE <seed-range> iterations=10000 failures=N`
  with the site-coverage table (how many hits per crash site — a torture
  run that only ever kills one site is a lottery, not a sweep).

## M28 — property-based testing (random schemas, data, queries)

Reuse, don't rebuild: the fuzzer's generators already do schema + data +
query synthesis (16-feature dialect). M28 turns them into *properties*:

1. **Round-trip:** generate N rows → insert all → read all back →
   byte-identical sets (values and storage classes via `typeof`).
2. **Delete-to-empty / reinsert:** write → delete all → table empty →
   reinsert same rows → same reads (freelist/M19-remainder interaction).
3. **Crash-consistency property:** property 1 with a random crash between
   write and read (composes M24's machinery with M28's generation).
4. **Equivalent-forms:** for generated queries, two syntactic forms with
   known-equivalent meanings (e.g. `WHERE NOT (a AND b)` vs De Morgan
   form, `LEFT JOIN … WHERE key IS NOT NULL` vs `INNER JOIN`) must agree
   on both engines — metamorphic checks where the reference oracle says
   the same thing differently-but-equal.
5. **Vacuum/idle invariance:** answers to a fixed query set identical
   before and after VACUUM on the same logical data (M19's semantics made
   into a property).

Acceptance: a run is `PROPERTIES seed=<s> cases=<n> failures=<n>` with
every failure minimized to a schema+data+query triple that replays, then
classified per the fuzz triage rules (engine bug / SQLite quirk /
harness defect) — same pipeline, second entry point.

## M25/M26/M27 — acceptance shapes (added: concurrency is not differential)

The corpus compares answers on identical inputs; concurrency is
timing-dependent, so byte-differential acceptance would measure scheduling
luck. These three get **invariant acceptance** (M24-style), stated now:

- **M25 snapshot isolation:** a reader that starts before a writer commits
  must finish seeing the pre-write state (no torn/dirty reads); readers
  after commit see the new state; two concurrent readers of the same data
  agree with each other and with some single committed state. Test by
  holding a read open across a commit and comparing, not by racing and
  diffing output order. Scope, stated for Default: single-writer
  concurrency (SQLite's own model) — multi-writer is out unless the
  ladder is amended.
- **M26 locking/deadlock:** the *model* is ours (one writer, wait-or-
  fail policy declared in the milestone); what's conformance is the
  **error text** when a second writer arrives — SQLite's
  `database is locked` wording is differentially checkable in a
  deliberately-constructed two-handle scenario (both engines, same
  script, error compared like any rejection case). If our locking model
  makes a scenario unreachable, that's a recorded scope flag, not a
  silent skip. Deadlock handling per se: with one writer there are no
  lock cycles — the milestone's honest deliverable is the *policy*, plus
  proof no two-handle scenario wedges instead of erroring.
- **M27 checksums + integrity:** per-page checksums are invisible until
  corruption; acceptance = (a) `PRAGMA integrity_check`-equivalent on a
  healthy file returns `ok` **and matches the reference's `ok` on the
  same file** (differential where determinism allows), (b) corruption
  fixtures (single flipped byte at a checksummed offset) are *detected*
  with a clear error and never silently returned as data — detection is
  the property; a crash-on-corruption counts as detection only if the
  error is typed and replayable, stated in the milestone report.

## Boundary with what exists

- Fuzz spec (M9) owns *mismatch rate against the reference*; this doc owns
  *crash invariants* (M24) and *generated-property coverage* (M28).
- Shared: generator code, seed logging, minimization, triage, red-push
  protocol. No second implementation of any of those — if M28 needs a new
  generator primitive, it lands in the shared generator, not a fork.
