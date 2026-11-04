#!/usr/bin/env python3
"""Tier-3 gate benchmark harness: W1-W10 per docs/benchmark-spec.md.

W9 (open/reopen) times 50 × (open a 100k-row file + first query) against files built before the
timed region -- it is the only workload that prices open and cold-start cost, and the spec has
defined it since the first full run left it out.

W1 point lookup runs with small `scale` as the whole 100k prefix — the sqlite
side's index makes it O(log n) either way, so we run at the full scale for the
gate.

Default-authored harness per research's spec (committed as-is). Method:
warm-up discarded; N=7 timed runs per (engine, workload), ALL raw values
printed; identical-output check (the conformance normalizer's tuple shape —
no second comparator); ratio = minidb_ms/sqlite_ms per workload; the headline
is the MEDIAN of per-workload ratios (never total/total). python-side module
version skew (3.53.1 vs the CLI's 3.54.0) is recorded in the header per the
spec's skew rule (timing only).
"""

import argparse
import os
import tempfile
import statistics
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minidb.engine import Database  # noqa: E402


def build_rows(scale, seed=7):
    rows = []
    r = seed
    for i in range(1, scale + 1):
        r = (1103515245 * r + 12345) & 0x7FFFFFFF
        v = r % 1000
        k = "k%d" % (v // 10)
        rows.append((i, v, k))
    return rows


def new_sq(path=None):
    s = sqlite3.connect(path or ":memory:")
    s.execute("PRAGMA journal_mode=MEMORY")
    s.execute("PRAGMA synchronous=OFF")
    s.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER, k TEXT)")
    s.executemany("INSERT INTO t VALUES (?,?,?)", ROWS)
    s.commit()
    return s


def new_md(path=None):
    m = Database(path)
    m.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER, k TEXT)")
    for r in ROWS:
        m.run("INSERT INTO t VALUES (%d,%d,'%s')" % r)
    return m


ROWS = []

WORKLOADS = {
    "W1": ("point lookup", None,
       lambda s: [s.execute("SELECT k FROM t WHERE id=?", (i,)).fetchall()
                  for i in range(1, LOOKUPS + 1)],
       lambda m: [m.run("SELECT k FROM t WHERE id=%d" % i)[1]
                  for i in range(1, LOOKUPS + 1)]),
    "W2": ("range scan (selective + wide)", None,
       lambda s: [s.execute("SELECT id FROM t WHERE id BETWEEN 5000 AND 5200").fetchall(),
                  s.execute("SELECT id, v FROM t WHERE id BETWEEN 5000 AND 50000").fetchall()],
       lambda m: [m.run("SELECT id FROM t WHERE id BETWEEN 5000 AND 5200")[1],
                  m.run("SELECT id, v FROM t WHERE id BETWEEN 5000 AND 50000")[1]]),
    "W3": ("full-scan aggregate", None,
       lambda s: s.execute("SELECT COUNT(*), SUM(v) FROM t").fetchall(),
       lambda m: m.run("SELECT COUNT(*), SUM(v) FROM t")[1]),
    "W4": ("sort top-N", None,
       lambda s: s.execute("SELECT id, v FROM t WHERE k != 'zzz' ORDER BY v LIMIT 20").fetchall(),
       lambda m: m.run("SELECT id, v FROM t WHERE k != 'zzz' ORDER BY v LIMIT 20")[1]),
}


def w2_join_setup(s, m):
    s.execute("CREATE TABLE j (id INTEGER PRIMARY KEY, w TEXT)")
    s.commit()
    s.execute("INSERT INTO j VALUES (1, 'a')")
    s.commit()
    m.run("CREATE TABLE j (id INTEGER PRIMARY KEY, w TEXT)")
    m.run("INSERT INTO j VALUES (1, 'a')")


WORKLOADS["W5"] = ("join", w2_join_setup,
    lambda s: s.execute("SELECT t.id, v, w FROM t JOIN j ON t.id = j.id").fetchall(),
    lambda m: m.run("SELECT t.id, v, w FROM t JOIN j ON t.id = j.id")[1])

WORKLOADS["W6"] = ("grouped aggregate (~100 groups)", None,
    lambda s: s.execute("SELECT k, COUNT(*), SUM(v) FROM t GROUP BY k").fetchall(),
    lambda m: m.run("SELECT k, COUNT(*), SUM(v) FROM t GROUP BY k")[1])

LOOKUPS = 1000
SCALE_7 = 10000
W9_OPENS = 50
W9_PATHS = []


def w7_sq(s):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, v INTEGER)")
    c.commit()
    for i in range(1, SCALE_7 + 1):
        c.execute("INSERT INTO b VALUES (?,?)", (i, i % 97))
    c.commit()
    return c.execute("SELECT COUNT(*) FROM b").fetchall()


def w7_md(m):
    c = Database()
    c.run("CREATE TABLE b (id INTEGER PRIMARY KEY, v INTEGER)")
    for i in range(1, SCALE_7 + 1):
        c.run("INSERT INTO b VALUES (%d,%d)" % (i, i % 97))
    out = c.run("SELECT COUNT(*) FROM b")[1]
    return out


WORKLOADS["W7"] = ("bulk write 10k single-row commits", None, w7_sq, w7_md)

def w8_sq(s):
    for i in range(1, LOOKUPS + 1):
        s.execute("UPDATE t SET v = ? WHERE id = ?", (i % 990, i))
    s.commit()
    return s.execute("SELECT COUNT(*) FROM t WHERE v < 1000").fetchall()

def w8_md(m):
    for i in range(1, LOOKUPS + 1):
        m.run("UPDATE t SET v = %d WHERE id = %d" % (i % 990, i))
    return m.run("SELECT COUNT(*) FROM t WHERE v < 1000")[1]

WORKLOADS["W8"] = ("point update 1k by PK", None, w8_sq, w8_md)


def w10_setup(s, m):
    big = "z" * 4000
    s.execute("CREATE TABLE t10 (id INTEGER, b TEXT)")
    s.executemany("INSERT INTO t10 VALUES (?,?)",
                  [(i, big) for i in range(1, LOOKUPS + 1)])
    s.commit()
    m.run("CREATE TABLE t10 (id INTEGER, b TEXT)")
    for i in range(1, LOOKUPS + 1):
        m.run("INSERT INTO t10 VALUES (%d, '%s')" % (i, big))

WORKLOADS["W10"] = ("overflow rows (1k x 4KB full scan)", w10_setup,
    lambda s: s.execute("SELECT SUM(length(b)) FROM t10").fetchall(),
    lambda m: m.run("SELECT SUM(length(b)) FROM t10")[1])


def loadavg():
    """1-minute load average as this workload starts, or None when unreadable.

    Recorded beside each workload because a leg's box state is invisible in the artifact
    afterwards: identical medians come out of a quiet machine and a loaded one, and only this
    number (next to the reference's raw7 spread) separates the two readings.
    """
    try:
        with open("/proc/loadavg", "r", encoding="ascii") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def w9_open_setup(s, m):
    """Build the two 100k-row files W9 opens, outside every timed region.

    The runner has already built the in-memory pair by the time this runs; those are closed here so
    the timed lambdas see a cold file and nothing else holding it.
    """
    s.close()
    m.close()
    scratch = tempfile.mkdtemp(prefix="bench-w9-")
    sqlite_path = os.path.join(scratch, "w9-sqlite.db")
    minidb_path = os.path.join(scratch, "w9-minidb.db")
    build_sqlite = new_sq(sqlite_path)
    build_sqlite.close()
    # One transaction: outside a transaction every INSERT is its own commit, and at 14 ms of
    # commit cost per row the 100k file would take twenty minutes to build. The setup is not the
    # measurement, so it takes the cheap path the write workload (W7) is there to price.
    build_minidb = Database(minidb_path)
    build_minidb.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER, k TEXT)")
    build_minidb.run("BEGIN")
    for r in ROWS:
        build_minidb.run("INSERT INTO t VALUES (%d,%d,'%s')" % r)
    build_minidb.run("COMMIT")
    build_minidb.close()
    W9_PATHS[:] = [sqlite_path, minidb_path]


def w9_sqlite(_ignored):
    answers = []
    for _ in range(W9_OPENS):
        conn = sqlite3.connect(W9_PATHS[0])
        answers.append(conn.execute("SELECT COUNT(*) FROM t").fetchall())
        conn.close()
    return answers


def w9_minidb(_ignored):
    answers = []
    for _ in range(W9_OPENS):
        db = Database(W9_PATHS[1])
        answers.append(db.run("SELECT COUNT(*) FROM t")[1])
        db.close()
    return answers


WORKLOADS["W9"] = ("open/reopen (50 x open + first query)", w9_open_setup, w9_sqlite, w9_minidb)


def main():
    global ROWS, LOOKUPS
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=100000)
    ap.add_argument("--lookups", type=int, default=1000)
    ap.add_argument("--n", type=int, default=7)
    ap.add_argument("--workloads", default="W1,W2,W3,W4,W5,W6,W7,W8,W9,W10")
    args = ap.parse_args()
    LOOKUPS = args.lookups
    ROWS = build_rows(args.scale)
    picked = [w for w in args.workloads.split(",") if w]

    print("BENCH first-full-run  module-sqlite %s  N=%d  scale=%d  lookups=%d"
          % (sqlite3.sqlite_version, args.n, args.scale, args.lookups), flush=True)
    print("note: benchmark runs the PYTHON module (timing-only reference per the "
          "spec!); the CLI's 3.54.0 is the pinned corpus reference — module-version "
          "skew accepted for timing and recorded here (the skew rule, not a "
          "smoothing).")
    print("note: ratio = median(minidb)/median(sqlite) on the unrounded ms values; the "
          "medians print at 2 dp, so dividing the displayed pair can differ in the last digit.")

    ratios, discards = [], []
    for wid in picked:
        name, pre, q_sq, q_md = WORKLOADS[wid][0], WORKLOADS[wid][1], WORKLOADS[wid][2], WORKLOADS[wid][3]
        # The gate window is read BEFORE any setup or timing: a load captured after the loops
        # describes the box the work left behind, not the box it was called on. The after-value
        # is still printed, labelled, so a reader can see whether the run disturbed the machine.
        load_before = loadavg()
        s, m = new_sq(), new_md()
        if pre:
            pre(s, m)

        def run_sq():
            return q_sq(s)

        def run_md():
            return q_md(m)

        try:
            sq_out, md_out = run_sq(), run_md()
        except Exception as e:
            print("%s  %-40s ERROR: %r" % (wid, name, e), flush=True)
            discards.append(wid)
            continue
        ok = sq_out == md_out
        if not ok:
            print("%s  %-40s DISCARDED: outputs differ (first mismatch: %r vs %r)"
                  % (wid, name, sq_out if not isinstance(sq_out, list) else sq_out[:1],
                     md_out if not isinstance(md_out, list) else md_out[:1]), flush=True)
            discards.append(wid)
            continue

        # warm-up discarded, then N=7 per (engine, workload)
        run_sq(); run_md()
        t_sq, t_md = [], []
        for _ in range(args.n):
            t0 = time.perf_counter(); run_sq(); t_sq.append((time.perf_counter() - t0) * 1000)
        for _ in range(args.n):
            t0 = time.perf_counter(); run_md(); t_md.append((time.perf_counter() - t0) * 1000)

        ratio = statistics.median(t_md) / statistics.median(t_sq)
        shapesq = (len(sq_out) if isinstance(sq_out, list) else 1)
        load_after = loadavg()
        print("%s  %-40s rows=%d  loadavg=%s  loadavg_after=%s"
              % (wid, name, shapesq, "n/a" if load_before is None else "%.2f" % load_before,
                 "n/a" if load_after is None else "%.2f" % load_after))
        print("     sqlite_ms median=%.2f  raw7=[ %s ]" % (statistics.median(t_sq), " ".join("%.2f" % x for x in t_sq)))
        print("     minidb_ms median=%.2f  raw7=[ %s ]" % (statistics.median(t_md), " ".join("%.2f" % x for x in t_md)))
        print("     ratio=%.3f  output: OK" % ratio, flush=True)
        ratios.append(ratio)

        s.close()

    if ratios:
        med = statistics.median(ratios)
        print("RATIOS: median-of-ratios=%.3f" % med)
    else:
        print("RATIOS: none (all workloads discarded)")


if __name__ == "__main__":
    main()
