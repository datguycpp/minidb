# Read-path prework (owner: Research) — UN-ORDERED candidate item

Not an ordered item. This file exists so the profile and the acceptance
structure survive scratch pruning (a quoted number whose file dies is a
number nobody can check later). Order decision is Steven's; if never
ordered, this is the complete handoff for whoever picks it up.

## Provenance

Profiled read-only on the 100k file W9 built (8.1 MB, ~1,972 pages),
same-shape repeats interleaved, box at loadavg ~3 (loud), no tree
movement, nothing committed by the profiling work itself.

## The measurement (two passes, numbers as printed)

Four-layer split:

```
open (Database(path), 100k file)          0.32 ms     0.3% of 411 ms
layer 1  B-tree page walk, no decode    156.75 ms     1.568 us/row    35%
layer 2  record decode alone            254.67 ms     2.547 us/row    57%
layer 3  walk+decode+tuple, as a list   423.02 ms     4.230 us/row
query    SELECT COUNT(*)                445.65 ms     4.457 us/row
                                                        aggregate ~0.23 us/row, ~2%
```

Layer 1 split:

```
load + CRC every page      18.48 ms     9.4 us/page    <- the I/O half
parse every page (_load)  102.56 ms    52.0 us/page    <- header + every cell's metadata
real tree.scan            143.57 ms    72.7 us/page   1.436 us/row
cell traversal (scan-parse) 41.00 ms    0.410 us/row
```

Noise envelope (18 interleaved same-state samples of the two aggregate
forms): median 378 ms, min 364, max 522 — a 42% span; low band tight
(min to median 4%), right tail +38%. Corroborating envelope point from
the record: the aliasing acceptance measured 1.077 ms (engineer) and
1.37 ms (hermes cold re-run) — 27% apart, two runs, same box class.

Discriminating probe (pre-registered before the profile): COUNT(*) vs
COUNT(*), SUM(v) differ by -73 ms (-16%), wrong direction for real
arithmetic — aggregate arithmetic is not the cost. Reading: decode +
materialisation wins; warm-cache I/O is weakest (confirmed: load+CRC
is 18 ms of the query).

## The rules the profile filed

- **Pay for what you consume, priced at three layers**: record decode
  254.67 ms (57%), node-object page parse 102.56 ms (23%), tuple/list
  11.6 ms (2.6%). The W8 enumeration rule generalises to all three; its
  tuple-free cure shape reaches only the 2.6% sliver, so the read side's
  real cures are the two candidates below.
- **Unpack once, not six**: 609,834 `_struct.unpack_from` calls over
  100k rows = 6.1 per three-column row.
- **Reference shape** (inference from timing): SQLite's 0.98 ms per
  open+query (~5 ns/row) cannot be record parsing — the reference counts
  page-level metadata for COUNT(*), which is the shape to match.

## Candidates and measured floors (Part B anchors)

- **(a) page-header count** (COUNT-shaped): reads each page's `nCell`,
  never parses a cell, never decodes a record. Floor = load+CRC
  (18.48 ms) + header parse → **~18–30 ms** for W9's first query
  (today ~411–446 ms). Pages carry cell counts as validated fields
  (M27's cell-count fixtures).
- **(b) decode path** (every on-disk scan): header-once unpack attacks
  the 254.67 ms block for all scans, not just COUNT. Floor unpriced —
  engineer declined to price it, deliberately.

## Acceptance structure (two parts; Part A amended with measured evidence)

- **Part A (frozen):** after the fix, a re-profile shows the *targeted
  block* down by more than the **calibrated noise envelope** — the
  envelope measured from same-state interleaved repeats (loadavg in-file,
  raw7/max/min printed, outlier count reported, medians compared).
  NOT a fixed percent: an 18-sample calibration spanned 42% on this box,
  so a fixed 16% gate would sit inside the noise (the original 16% came
  from two point comparisons — a wobble estimate, not an envelope). The
  threshold number is the calibration's own median-spread, declared
  before the fix is measured; the calibration measures noise, never the
  effect, so it cannot game the gate.
- **Part B (frozen at ordering, with the chosen shape):** W9's first
  query at 100k under the full protocol (N=7, loadavg in-file), anchored
  to the chosen candidate's profiled floor plus a margin outside the
  envelope. Secondary lines: the 4.1 and 7.8 us/row scan constants
  re-measured so benefits beyond W9 show.

## Status

Un-ordered; the record is complete without it (zero open items stands).
Candidates (a) and (b) have measured floors and disjoint scopes —
(a) COUNT-shaped, (b) every on-disk scan — so the order should name the
scope and Part B becomes arithmetic at that point.
