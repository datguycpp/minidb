# MiniDB fuzzing spec (owner: Research; delivered before M9 starts)

Milestone 9 ("fuzzing against SQLite") and the Tier-2 gate's
"fewer than 1 mismatch per 10,000 queries" both need a defined query source
and a defined mismatch. This spec is that definition. Written before the
milestone, per the standing rule: authored before the work, not after.

## What counts as a mismatch

One thing only: **MiniDB's answer for a generated input differs from the
pinned SQLite 3.54.0's answer for the same input**, judged by the
conformance harness's normalizer (`docs/conformance-spec.md` — same error
framing strip, same typed-JSON comparison, same no-sort rule). The fuzzer
does not get its own comparison logic; a second normalizer is a second
truth. Syntax-level fuzz mismatches are byte-identical rejection
disagreements, counted in the same rate.

## Oracles, in order of use

1. **Differential vs pinned SQLite (primary, Tier-1 M9 scope).** Generate a
   schema + rows + queries in the dialect both engines accept; any output
   or error-text divergence is a mismatch. With a reference this strong,
   differential is the whole oracle — NoREC's value (catching optimization
   bugs *in the reference*) does not apply when the reference defines
   correctness.
2. **TLP — Ternary Logic Partitioning (secondary, staged by measurement).**
   Reference-free consistency on MiniDB alone: `p` vs `p IS NULL` vs
   `p IS NOT NULL` partitions must sum to the whole. Finds logic bugs even
   where SQLite and MiniDB agree wrongly on quirks. Rerunner of record:
   [Rigger & Su, OOPSLA 2020](https://doi.org/10.1145/3428279).
3. **NoREC — NOT run by default.** [Cost finding](https://github.com/sqlancer/sqlancer/):
   its non-optimizing rewrites are self-join shaped and M8's join is
   scan×scan, so it prices badly against this engine. Include only behind a
   measured budget line (small fixtures, run-capped); if the measurement
   says it costs more than TLP per mismatch found, the spec's record says
   so and it stays off.

## Generator scope

- **Subset-aware:** the generator emits only syntax SQLite accepts (so a
  mismatch can't be dismissed as invalid input) and is seeded from the
  milestone-supported feature set at run time — declared in the run header
  (`features: …`) so a rate from a narrower dialect is never compared to
  one from a wider dialect.
- **Two generators, both cheap:**
  - *Structured:* random schemas (1–4 tables, 0–100 rows, NULLs and type
    variety forced), then random WHERE/JOIN/aggregate/subquery queries.
  - *Mutation:* take corpus cases and mutate (literal swaps, operator
    flips, clause drops) — the fastest route to known-then-still-broken
    territory.
- **Rejection fuzz:** random token soup. Both engines must reject with
  byte-identical messages or it's a syntax mismatch. This is how the
  `1e`-style crashes (uncaught exceptions) get hunted: **any MiniDB
  traceback is a mismatch regardless of SQLite's answer** — an exception is
  never a valid response.

## Stateful crash seeds (from M7's evidence)

Seed scenarios, not generator output: write → kill at `MINIDB_CRASH_POINT`
(both sides of the fsync) → reopen → query, reusing engineer's seam rather
than a second fault-injection mechanism. Covers M7's named two: torn frame
and first-commit-on-empty-file. Any state divergence after recovery is a
mismatch with the same triage path.

## Triage and the mismatch rate

- **Every mismatch must minimize**: shrink schema/rows/statements until the
  mismatch disappears, print the minimized repro + seed + both outputs
  verbatim. An unminimized mismatch is a lead, not a filed bug.
- Minimized repros become corpus cases (the fuzzer's output feeds the
  denominator's next tranche — that is the loop: fuzz → minimize → corpus →
  regression-pinned).
- Classification before fixing: engine bug (owned by its milestone) /
  SQLite quirk (ground truth — we match) / harness-normalizer defect
  (re-baseline event, not a fix). Never "fuzzer bug" without a repro.
- **Rate = mismatches ÷ queries × 10,000**, reported raw with total queries,
  dialect features, and seed range. Tier-2's gate reads this number;
  Tier-1's M9 acceptance requires the pipeline runs end-to-end and reports,
  with zero unfixed-and-unfiled mismatches at report time.

## Budgets (cost caps, checked before the milestone starts)

- Table size ≤ 100 rows (keeps M8's scan×scan join affordable — engineer's
  pricing requirement); per-query timeout; per-run query floor of 10,000
  for gate-number runs, lower for smoke runs (labeled, never quoted as a
  gate number).
- Deterministic: every run logs its seed; a reported mismatch must replay
  from the log line alone.

## Acceptance shape for M9 (raw, as always)

```
FUZZ run <seed-range>: queries=N  mismatches=M  rate=M/N*10000 per 10k
per-oracle: differential / TLP / (NoREC: off|on, measured cost)
each mismatch: minimized repro + classification + owner (or corpus case)
```

No summary line without the repros. If M runs 0 mismatches over 10k+
queries, the raw count and the dialect features are still posted — "clean"
without a dialect scope is unverifiable.
