# MiniDB conformance scoring spec (owner: Research)

Ratified by Default at M1. This file defines what the score line means.
Engine code never touches it; the runner is the only consumer.

## The four ratified decisions (+1 amendment)

1. **Reference is pinned: SQLite 3.54.0**, via the `sqlite3` CLI (not the
   Python `sqlite3` module — on this box that module is 3.53.1 and would
   silently break comparability). The runner prints `*** PINNED IS 3.54.0 ***`
   whenever the live binary differs; a run with that banner does not count.
2. **Not implemented = failed.** The denominator is never shrunk to make the
   score rise. A feature the milestone hasn't reached fails when its case
   appears in the corpus.
3. **SQLite quirks are ground truth.** Where SQLite is weird, matching the
   weirdness is passing. Divergences we choose on purpose are recorded as
   `known divergences` in STATE.md — still failures in the score.
4. **Normalization is harness-owned** (this spec + the runner). Neither the
   engine nor the milestone's own tests may redefine what "same result"
   means.
5. **Amendment (Default):** any normalization change re-runs the full corpus
   and reports the before/after score delta in the same STATE.md entry.
   A normalizer edit that silently reshuffles history is metric drift, not
   progress.

## Case format (adopted from `tools/behavior_conformance.py`)

- Plain `.txt` files under `tests/corpus/`.
- Cases separated by a blank line; each case runs in a **fresh database**.
- `--` lines are comments; case headers carry provenance (`-- src:`, `-- tier:`).
- The last statement should be a `SELECT` (its columns and rows are compared).
  If any statement errors, **both engines must fail with byte-identical
  messages** (after CLI-framing strip). Agreement on rejections counts.
- Expected results are **not written in the file**: they are computed at run
  time against the pinned binary. This is the differential design and it is
  why a corpus case can never rot.

## Normalization rules (runner-owned, exhaustive)

- SQLite side runs through `sqlite3 -json` → typed JSON values.
- CLI error framing (`Error:`, `Parse error:`, `Runtime error near line N:`)
  is stripped; SQLite's complaint text is compared verbatim to ours.
- Columns compared as an ordered list of names (only when both sides have
  columns).
- Rows compared **in returned order**. No sorting is applied. A case that
  needs order-insensitivity must say `-- sort` (sorts both sides' rows by
  JSON repr before compare) — added when a tier needs it, never silently.
- Numbers compare as Python floats parsed from JSON (exact equality after
  round-trip). No epsilon, no rounding. If float formatting ever diverges,
  that is a reported failure until a normalizer change with a logged delta
  says otherwise.
- NULL comes through JSON as `null` on both sides.

## Score line format

```
SCORE <tier>.<tranche>: <passed>/<total> (<pct>%) [Δ <signed> vs <baseline>]
```

- `passed/total` over the **current** corpus for that suite.
- Every reported score names its baseline and its corpus revision
  (git commit of `tests/corpus/`), so a number can always be reproduced.
- Suites are tier-scoped: gate percentages apply to that tier's suite only
  (Tier 1 gate = 90% of the tier-1 suites, etc.).

## Re-baseline events (require a STATE.md entry with before/after)

- Corpus file added, removed, or edited.
- Normalization rule changed.
- SQLite pin changed.
  The entry records: old score, new score, count of cases added/removed,
  and why. Two scores from different corpus revisions are never compared
  without this entry.

## Corpus provenance and tranche plan

Two sources, both pinned:

1. **Authored cases** (`tests/corpus/t1a.txt`, …) — written here, case
   headers say `-- src: authored`.
2. **Imported slt cases** — from `sqlite-sqllogictest-corpus` (the format
   [originally built to test SQLite](https://www.sqlite.org/sqllogictest/doc/trunk/about.wiki)),
   imported at a **pinned commit** with a converter into this case format.
   File headers record repo + commit + upstream path. Not yet imported;
   conversion is a runner-adjacent tool, spec'd here and built when tier-1
   gates need the volume.

Tranches, in ladder order:

| Tranche | Covers | Lands |
| --- | --- | --- |
| `t1a` | DDL/DML/SELECT-WHERE basics, rejection agreement, in-scope deferred failures | now |
| `t1b` | storage/index observable behaviour (milestones 3–5) | after M5 |
| `t1c` | joins, aggregates, subqueries (milestone 8) | after M8 |
| `t1d` | transactions + crash-visible semantics (milestones 6–7) | after M7 |
| `t1e` | fuzz corpus seeds (milestone 9) | after M9 |
| tier-2..6 | per ladder, mapped to milestones 10+ | as gates approach |

Cases for milestones not yet built are **deliberately in the corpus now or
when their tranche is published** — they fail, and that is the point
(decision 2). Tranche scope is about ladder ownership, not about hiding
failures.

## Runner interface (for the milestone's harness to expose)

- `run_corpus` globs `tests/corpus/t*.txt`, runs every case, prints per-case
  failures with raw expected/actual, then the score line above.
- The score line output also prints the **corpus revision** (short git commit
  of the corpus files) and the **SQLite pin**, so any posted number can be
  reproduced without trusting the person who posted it.
- Exit code non-zero on any failure.
- Raw output goes in the milestone report verbatim. "All good" is not a
  report.
