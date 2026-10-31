#!/usr/bin/env python3
"""Tier-3 gate benchmark harness: W2, W3, W4, W5, W6, W7, W8, W10 per
docs/benchmark-spec.md. (W1 and W9 are also implemented, see note.)

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


def main():
    global ROWS, LOOKUPS
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=100000)
    ap.add_argument("--lookups", type=int, default=1000)
    ap.add_argument("--n", type=int, default=7)
    ap.add_argument("--workloads", default="W1,W2,W3,W4,W5,W6,W7,W8,W10")
    args = ap.parse_args()
    LOOKUPS = args.lookups
    ROWS = build_rows(args.scale)
    picked = [w for w in args.workloads.split(",") if w]

    print("BENCH first-full-run  module-sqlite %s  N=%d  scale=%d  lookups=%d"
          % (sqlite3.sqlite_version, args.n, args.scale, args.lookups))
    print("note: benchmark runs the PYTHON module (timing-only reference per the "
          "spec!); the CLI's 3.54.0 is the pinned corpus reference — module-version "
          "skew accepted for timing and recorded here (the skew rule, not a "
          "smoothing).")

    ratios, discards = [], []
    for wid in picked:
        name, pre, q_sq, q_md = WORKLOADS[wid][0], WORKLOADS[wid][1], WORKLOADS[wid][2], WORKLOADS[wid][3]
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
            print("%s  %-40s ERROR: %r" % (wid, name, e))
            discards.append(wid)
            continue
        ok = sq_out == md_out
        if not ok:
            print("%s  %-40s DISCARDED: outputs differ (first mismatch: %r vs %r)"
                  % (wid, name, sq_out if not isinstance(sq_out, list) else sq_out[:1],
                     md_out if not isinstance(md_out, list) else md_out[:1]))
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
        print("%s  %-40s rows=%d" % (wid, name, shapesq))
        print("     sqlite_ms median=%.2f  raw7=[ %s ]" % (statistics.median(t_sq), " ".join("%.2f" % x for x in t_sq)))
        print("     minidb_ms median=%.2f  raw7=[ %s ]" % (statistics.median(t_md), " ".join("%.2f" % x for x in t_md)))
        print("     ratio=%.3f  output: OK" % ratio)
        ratios.append(ratio)

        s.close()

    if ratios:
        med = statistics.median(ratios)
        print("RATIOS: median-of-ratios=%.3f" % med)
    else:
        print("RATIOS: none (all workloads discarded)")


if __name__ == "__main__":
    main()
