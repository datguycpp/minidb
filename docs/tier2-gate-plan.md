# Tier-2 gate verification plan (owner: Research; written before M17's review)

The gate fires when M17 lands (last Tier-2 milestone; t2f's landing rides
the same review per the recorded ruling). This fixes the procedure and the
predictions BEFORE the run, so the gate is executed, not negotiated.

## The arithmetic, pre-computed

- Full corpus at gate: **208 cases** (187 current + t2f's 21).
- Tier-2 slice (t2a–f): **122 cases** (20+21+21+19+20+21).
- Tier-1 regression line (t1a–e + behavior): **86 cases must stay 86.**
- Gate threshold: **slice ≥ 95% = at least 116/122** on the slice, with the
  full 208 reported alongside and the 86-line explicitly checked (the
  no-regression condition from the recorded recommendation — a gate that
  can pass while old cases rot measures nothing).

## Checks, in order, all from a cold public clone

1. **Corpus run, full output:** per-file counts (behavior, t1a–f), the
   total, corpus revision (commit — tree claims name the commit or they
   don't count), SQLite pin banner.
2. **Slice arithmetic done by hand from the printed per-file lines**, not
   read off a tool's own summary — two independent computations of the
   same number, like every other number in this project.
3. **Syntax suite + durability + crash + transactions + plan tools:** green,
   unchanged from their milestone baselines.
4. **Fuzz gate run (semantic class):**
   - ≥ 10,000 queries, **semantic class only** (structured + mutation
     generators; rejection generator excluded from the gate rate per the
     ratified split, still run and reported separately).
   - Header must declare: seed, mode, dialect features (which milestone
     surfaces the generator exercised — triggers, windows, set ops and
     affinity are all new since M9's runs), commit, and query count.
   - **Gate number: semantic mismatches per 10,000 < 1.** Every mismatch
     minimized + classified before the run can be quoted.
   - Rejection-position rate: printed raw in the same block; this run is
     its **baseline as a gate metric** (first gate under the split), so
     "non-increasing" binds from the *next* gate onward.
   - Steven's ratification of the split is still the one open human item;
     if it hasn't landed, the run proceeds and the result is filed as
     provisional — the literal-wording gate would read the total line.
5. **Every mismatch or failed case before the gate number:** classified
   (engine bug / SQLite quirk / normalizer defect) with an owner — a gate
   that reports a percentage over unclassified failures is the exact
   failure mode the project's rules were written against.

## Pre-registered predictions (posted before the run)

- **Slice: 119–122/122.** Anchored on my t2f prediction (17–20/21); below
  119 = a t2f miss bigger than declared swings, below 116 = gate NOT MET,
  which posts as NOT MET with the raw lines — no threshold edits, no
  denominator moves.
- **Full corpus: 205–208/208**, with the 86-line intact (a Tier-1
  regression found here is a finding of its own, reported above the gate
  verdict, not folded into it).
- **Fuzz semantic rate: 0/10k predicted**, with the named risk class:
  generator surfaces added after M15 (triggers, affinity edge cases,
  window frames) producing mismatches in features the engine has had one
  milestone of exposure to. If the rate lands ≥1: it is what it is —
  minimized repros, owners, and the gate verdict follows the number.

## What is NOT allowed at this gate

- Adding, rewording or re-classifying cases between the run and the verdict.
- Setting or adjusting the fuzz baseline by observation (baselines are set
  by the first declared run, like the benchmark multiple: before fixes).
- Quoting a percentage without its per-file lines and corpus revision.
- Reporting the slice without the full-corpus line beside it.
