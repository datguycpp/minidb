#!/usr/bin/env python3
"""W9's cycle split into open cost and the COUNT(*) walk, at 100k rows, N timed samples
(default 11 — the frozen floor for a gate calibration), one file.

Gate 2's remaining gap lives in the two parts of the cycle candidate (a) did not remove: opening
the file, and the page walk `count_rows` still performs. This tool measures both sides of that
split on the same file, at the workload's own scale (50 repetitions per timed run), so the next
lever has an address instead of a direction:

    open        -- Database(path); close, no query
    open+count  -- the W9 cycle itself: Database(path); SELECT COUNT(*) FROM t; close

The count share is the difference between the two medians, which keeps the page cache honest: both
shapes open a cold file, so neither is measured warm. This is a component measurement, not gate 2 —
the reference engine is not in this tool, and no ratio is printed.

Load is read immediately before **every** sample, not once per group: a single before/after pair
cannot attribute a drift-shaped raw sample set to the box, which is exactly the artefact this
instrument has to rule out before its numbers can judge a change. Each shape also prints its own
band, (max - min) / median, because that band is what a later win has to clear.
"""

import argparse
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.engine import Database  # noqa: E402

REPEATS = 50


def loadavg():
    try:
        with open("/proc/loadavg", "r", encoding="ascii") as handle:
            return float(handle.read().split()[0])
    except OSError:
        return None


def build(path, rows):
    """One 100k file, built before any timed region (same shape as the W9 fixture)."""
    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER, k TEXT)")
    db.run("BEGIN")
    for i in range(1, rows + 1):
        db.run("INSERT INTO t VALUES (%d,%d,'k%d')" % (i, i % 1000, i % 100))
    db.run("COMMIT")
    db.close()


def open_only(path):
    for _ in range(REPEATS):
        Database(path).close()


def open_and_count(path):
    for _ in range(REPEATS):
        db = Database(path)
        db.run("SELECT COUNT(*) FROM t")
        db.close()


def time_shape(fn, path):
    start = time.perf_counter()
    fn(path)
    return (time.perf_counter() - start) * 1000


def timed_runs(fn, path, count):
    """N samples, each paired with the load read immediately before it.

    One reading before the whole group cannot attribute a drift-shaped raw sample set; one per
    sample can.
    The reading is taken before the sample, so a slow sample carries the box state it ran in.
    """
    values, loads = [], []
    for _ in range(count):
        load = loadavg()
        start = time.perf_counter()
        fn(path)
        values.append((time.perf_counter() - start) * 1000)
        loads.append(load)
    return values, loads


def band_percent(values):
    """(max - min) / median: the calibration's own spread, the band a win must clear."""
    median = statistics.median(values)
    return 0.0 if median == 0 else (max(values) - min(values)) / median * 100.0


def settle(cap=180.0):
    """Wait for the build's own load to decay before the timed region.

    The 100k build is CPU-heavy and raises the very 1-minute average the run is about to be judged
    by: a reading taken straight after it measures the setup, not the box the samples run on. Poll
    until the load is back inside the frozen [1.0, 3.0] window, or the cap expires.
    """
    start = time.time()
    while True:
        current = loadavg()
        if current is not None and current <= 3.0:
            return current, time.time() - start
        if time.time() - start >= cap:
            return current, time.time() - start
        time.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--n", type=int, default=11,
                    help="timed samples per shape (default 11, the frozen floor for a gate "
                         "calibration; N<11 cannot certify one)")
    args = ap.parse_args()
    if args.n < 11:
        print("note: N=%d is below the frozen floor of 11 -- this run can measure, not certify."
              % args.n)

    path = os.path.join(tempfile.mkdtemp(prefix="w9-split-"), "w9.db")
    build(path, args.rows)
    size = os.path.getsize(path)

    load_before, waited = settle()
    print("settled: %.0fs after the build, loadavg=%s"
          % (waited, "n/a" if load_before is None else "%.2f" % load_before))
    if load_before is None or not (1.0 <= load_before <= 3.0):
        print("WINDOW NOT MET: this run cannot calibrate the gate band; label it non-gate evidence.")
    print("W9 SPLIT  rows=%d  repeats=%d  N=%d  file_bytes=%d" % (args.rows, REPEATS, args.n, size))
    print("note: component measurement, not gate 2 -- no reference engine, no ratio; counts are a "
          "fresh open each cycle, so neither shape is measured warm.")

    time_shape(open_only, path)
    time_shape(open_and_count, path)
    t_open, open_loads = timed_runs(open_only, path, args.n)
    t_both, both_loads = timed_runs(open_and_count, path, args.n)

    load_after = loadavg()
    med_open = statistics.median(t_open)
    med_both = statistics.median(t_both)
    print("loadavg=%s  loadavg_after=%s"
          % ("n/a" if load_before is None else "%.2f" % load_before,
             "n/a" if load_after is None else "%.2f" % load_after))
    print("     open_ms median=%.2f  raw=[ %s ]  band=%.1f%%  per_open=%.2f"
          % (med_open, " ".join("%.2f" % x for x in t_open), band_percent(t_open),
             med_open / REPEATS))
    print("     open_loads=[ %s ]"
          % " ".join("n/a" if x is None else "%.2f" % x for x in open_loads))
    print("     open_count_ms median=%.2f  raw=[ %s ]  band=%.1f%%  per_cycle=%.2f"
          % (med_both, " ".join("%.2f" % x for x in t_both), band_percent(t_both),
             med_both / REPEATS))
    print("     open_count_loads=[ %s ]"
          % " ".join("n/a" if x is None else "%.2f" % x for x in both_loads))
    print("     count_share_ms=%.2f  per_cycle=%.2f  (open_count - open, medians)"
          % (med_both - med_open, (med_both - med_open) / REPEATS))


if __name__ == "__main__":
    main()
