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

The `load + CRC` row's "I/O" label is load-plus-verification: every
uncached page read runs `zlib.crc32` over the payload against its
stored trailer (`pager.read`, pager.py:216-237), so the 9.4 us/page is
dominated by checksum verification, not by disk time on a warm cache —
see candidate (a), where that becomes the floor's substance.

Noise envelope (18 interleaved same-state samples of the two aggregate
forms): median 378 ms, min 364, max 522 — a 42% span; low band tight
(min to median 4%), right tail +38%. Corroborating envelope point from
the record: the aliasing acceptance measured 1.077 ms (engineer) and
1.37 ms (hermes cold re-run) — 27% apart, two runs, same box class.

Discriminating probe (pre-registered before the profile): COUNT(*) vs
COUNT(*), SUM(v) differ by -73 ms (-16%), wrong direction for real
arithmetic — aggregate arithmetic is not the cost. Reading (1) wins as
pre-registered ("decode or materialisation"); within it, decode
dominates (57%) and tuple/list materialisation is 2.6% and was NOT the
cost — the W8-continuity line was corrected accordingly. Warm-cache I/O
is weakest (confirmed: load+CRC is 18 ms of the query).

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

## Candidates and floors (Part B anchors)

- **(a) page-header count** (COUNT-shaped): the page header carries
  everything a header-only count needs in its first three bytes —
  `kind = data[0]`, `count = struct.unpack_from("<H", data, 1)`,
  `btree.py:57-60` — so the shape is: read the page through the pager
  (where the measured 9.4 µs/page already comes from), take three bytes
  from the already-loaded buffer (no extra I/O), keep it if it is a
  leaf. No cell parse, no key compare, no payload decode. Three
  conditions make the design total: **leaf-only** (an interior page's
  count is separator pointers, not rows — the discriminator is the kind
  byte already in hand); **tree-scoped** (filtering the whole file's
  page range would also count the catalog's leaves — off by the
  catalog's rows on a query whose point is exact-and-cheap — and, in a
  general file, overflow/freelist pages whose first byte can collide;
  so the target tree's leaves are enumerated via its interior child
  pointers: ~7 interior pages at 100k (12-byte cells, fanout ~340),
  sub-millisecond, then header-sum the ~2,000 leaves); and
  **integrity-preserving** (the path reduces work *above*
  `pager.read()` — pager.py:216-237 — never below it: that call serves
  dirty pages first, refuses truncated or corrupt pages, and verifies
  the v2 CRC on every access, so a direct-seek count would be faster
  and wrong three ways — miss this session's dirty pages, read pages
  `read()` refuses, skip the checksum the format promises — while
  dirty-page consistency and corruption detection ride along free).
  Floor = leaf load+CRC (18.48 ms, dominated by checksum verification
  rather than disk time) + interior descent (~0.5 ms) → **~19 ms**
  (today ~411–446 ms). Pages carry cell counts as validated fields
  (M27's cell-count fixtures). The floor is arithmetic on this file —
  and it is M27's integrity priced once per page: the one number in
  this brief not to optimise, because shaving it trades checksums for
  speed.
- **(b) decode path** (every on-disk scan): header-once unpack attacks
  the 254.67 ms block for all scans, not just COUNT. Floor is a design
  parameter, not a file property: it moves with how much of the
  per-column unpack loop the chosen shape removes, and is declared at
  ordering with that shape — an order naming (b) states its own target,
  while an order naming (a) inherits an arithmetic floor.

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
  to the chosen candidate's floor — arithmetic for (a), a declared
  target for (b) — plus a margin outside the envelope. For (a) the
  target sits above the integrity floor with room, so no gate pressure
  ever points below `pager.read()`. Secondary lines:
  the 4.1 and 7.8 us/row scan constants re-measured so benefits beyond
  W9 show.

## Status

Un-ordered; the record is complete without it (zero open items stands).
Candidates (a) and (b) have disjoint scopes: (a) COUNT-shaped with a
floor measurable from this file (~19 ms), (b) every on-disk scan with
a floor that is a design parameter declared at ordering. The order names
the scope; Part B then inherits arithmetic for (a) or records the
declared target for (b).
