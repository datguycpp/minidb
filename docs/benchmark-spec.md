# MiniDB benchmark set spec (owner: Research)

Research's charter lane includes "the benchmark set". This defines it
BEFORE Tier 3 starts, so the Tier-3 gate ("benchmark queries run within an
agreed multiple of real SQLite's speed, with identical output; Default sets
the multiple after the first measurement") is measured by a written rule
instead of a convenient one.

## Two harnesses, on purpose (and why they differ from conformance)

- **Conformance** uses the pinned `sqlite3` **CLI** (decision 1): correctness
  needs the exact reference version, subprocess overhead is irrelevant.
- **Benchmarks** run **in-process on both sides**: Python `sqlite3` module
  vs MiniDB. Subprocess-per-query would measure fork/exec against a C
  library and flatter nobody. Cost: the module lags the pin (3.53.1 vs
  3.54.0 on this box) — **accepted for timing only**, recorded in every
  benchmark header. Version skew cannot matter at Python-vs-C distances;
  if a measured ratio ever makes it look like it might, that is a finding,
  recorded not smoothed.
  Rule: **the benchmark harness never decides correctness** — every timed
  query first passes the identical-output check (below). Speed of a wrong
  answer is not a score.

## Identical output: the precondition, not a metric

Each benchmark query runs the output comparison first (same rows, same
order, same columns — via the conformance normalizer, no second
comparator). Fail output → the query's timing is **discarded**, not
averaged. A benchmark with a wrong result gets the Tier-3 gate result
"NOT MET (output)" before any ratio is read.

## The set: workloads, sizes, and what they're for

Data generated deterministically (fixed seed, sizes below) into both
engines from one generator — same logical rows, both formats.

| # | Workload | Shape | Exercises |
| --- | --- | --- | --- |
| W1 | point lookup | `WHERE id = ?` on PK/index, 100k rows, 1k lookups | index seek (M4/M21) |
| W2 | range scan | `WHERE id BETWEEN ? AND ?`, selective + wide | B-tree range |
| W3 | full-scan aggregate | `COUNT/SUM` over 100k, no index | storage read path, page cache (M18) |
| W4 | sort top-N | `ORDER BY v LIMIT 20`, 100k rows | sort + memory (M18) |
| W5 | join | equijoin 1k × 1k (nested-loop today, index-driven post-M21) | planner evolution |
| W6 | grouped aggregate | `GROUP BY k` over 100k, ~100 groups | hash/sort grouping |
| W7 | bulk write | 10k single-row inserts, committed | write path, WAL (M7), freelist (M19) |
| W8 | point update | 1k updates by PK on 100k rows | update path (M16 disk) |
| W9 | open/reopen | open + first query on a 100k-row file, ×50 | recovery scan cost (M7), cache cold start |
| W10 | overflow rows | 1k rows of 4KB text, full scan (M20 in scope) | overflow pages |

- Sizes are fixed in the spec so a "faster" run can't come from smaller
  data. Adding a workload later = spec edit + note, never silent.
- W10 lands with milestone 20; workloads before their milestone's feature
  exists report `not-yet-runnable`, not zero.

## Method

- **Warm-up run discarded, then N=7 timed runs per (engine, workload);
  report median and all 7 raw values.** Medians, because one scheduler
  hiccup must not move a gate number.
- Machine state noted (this box, idle/not — recorded per run).
- Ratio = minidb_ms ÷ sqlite_ms **per workload**, reported individually and
  as the median of per-workload ratios (never total-time/total-time: W7
  would drown everything).
- **Score line shape:**

```
BENCH <run-id> <date>  module-sqlite <version>  N=7 median
workload  rows   sqlite_ms (raw 7)   minidb_ms (raw 7)   ratio   output: OK|DISCARDED
RATIOS: W1=… W2=… …   MEDIAN-RATIO=…
```

## Setting the multiple (Default's call, method fixed here)

- First full run establishes the baseline; Default reads the median-of-
  ratios plus per-workload outliers and sets the Tier-3 gate multiple
  from it, **written into STATE.md before further milestones land** —
  set after measurement, never after a fix (post-fix threshold-setting is
  metric gaming by another name).
- The gate reads: every workload's output OK, and median ratio ≤ the
  recorded multiple, with **no single workload allowed to sit above a
  recorded per-workload ceiling** — one pathological query can't hide
  inside an acceptable median.
- Honest-framing requirement (engineer's M-caveat, ratified): the multiple
  will be large for a Python engine. It is stated as measured, not
  softened; milestone 38 (hot-path rewrite) is the answer to a bad ratio,
  not a threshold edit.

## Non-goals

- Not a competitor benchmark: no other engines, no TPC claims.
- Not a conformance tool: if output mismatches, this harness discards
  timing and hands the query to the corpus triage path instead.
- Not micro-benchmarking expressions (parser/VM noise at these distances);
  workload-level only.
