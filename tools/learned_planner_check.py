#!/usr/bin/env python3
"""M37 learned-planner evidence: what history buys here, and what it cannot.

The declared acceptance (docs/tier6-prework.md, M37): outputs are the conformance surface,
choices are not; plan_check's riders (right answer, declared access path, estimated vs actual)
plus a before/after delta on repeated query sets. A planner that changes answers fails even if
its plans get faster — so every number below is paired with a row comparison.

Two measured facts decide the milestone:

1. The estimate is exact (a hash index reports its bucket size), so history cannot improve a
   choice. What it can remove is the repeated O(distinct keys) range probe: a repeated narrow
   range plans in 11.9 ms instead of 21.9 ms, rows identical.
2. The cost rule charged covering paths for random row fetches they never make — the M21 rule
   predates M22's covering indexes. A wide covering range is 22 ms against a 190 ms scan.

    python3 tools/learned_planner_check.py [--rows N]
"""

import argparse
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402


def timed(db, sql):
    start = time.perf_counter()
    rows = db.run(sql)[1]
    return (time.perf_counter() - start) * 1000, rows, db.last_plan


def median_run(db, sql, runs=3):
    values = [timed(db, sql)[0] for _ in range(runs)]
    return statistics.median(values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=20000)
    args = parser.parse_args()

    db = Database()
    db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
    db.run("BEGIN")
    for i in range(1, args.rows + 1):
        db.run(f"INSERT INTO t VALUES ({i}, {i})")
    db.run("COMMIT")
    db.run("CREATE INDEX idx_v ON t (v)")
    print("LEARNED PLANNER  (history is per-session; any write clears it)")
    print(f"  rows / distinct index keys : {args.rows:,} / {len(db.tables['t'].indexes['idx_v'].map):,}")

    # 1. the probe is what history removes
    index = db.tables["t"].indexes["idx_v"]
    probes = []
    for _ in range(5):
        start = time.perf_counter()
        index.lookup_range(">=", args.rows - 1000)
        probes.append((time.perf_counter() - start) * 1000)
    probe_ms = statistics.median(probes)

    # 2. repeated narrow range: fresh plan vs remembered plan, same rows
    narrow = f"SELECT v FROM t WHERE v >= {args.rows - 10}"
    first_ms, first_rows, first_plan = timed(db, narrow)
    remembered = [timed(db, narrow) for _ in range(4)]
    remembered_ms = statistics.median([r[0] for r in remembered])
    same_rows = all(r[1] == first_rows for r in remembered)
    print(f"  probe cost                 : lookup_range over {args.rows:,} keys: {probe_ms:.1f} ms")
    print(f"  repeated narrow range      : {first_ms:.1f} ms fresh -> {remembered_ms:.1f} ms "
          f"remembered ({(first_ms - remembered_ms) / first_ms * 100:+.0f}%)")
    print(f"  provenance                 : run 1 remembered={first_plan['remembered']}, "
          f"runs 2-5 remembered={all(r[2]['remembered'] for r in remembered)}; "
          f"answers identical: {same_rows}")

    # 2b. why a shape average cannot replace the probe: one shape, two literal extremes
    narrow_rows = len(db.run(narrow)[1])
    whole_rows = len(db.run("SELECT v FROM t WHERE v >= 0")[1])
    print(f"  shape spread               : same (table, column, >=) shape plans "
          f"{narrow_rows} rows at one literal and {whole_rows:,} at another")

    # 3. a write clears the memory, which is what makes it safe
    db.run("INSERT INTO t VALUES (0, 0)")
    after_write = timed(db, narrow)[2]
    print(f"  after a write              : remembered={after_write['remembered']} "
          f"(a remembered plan carries the rowids it was chosen from)")

    # 4. covering cost rule: the same projection, with and without the predicate
    wide = f"SELECT v FROM t WHERE v >= {args.rows - 1000}"
    median_run(db, wide)                                   # warm-up, discarded
    index_ms = median_run(db, wide)
    scan_ms = median_run(db, "SELECT v FROM t")            # no predicate: the scan baseline
    plan = timed(db, wide)[2]
    print(f"  covering cost rule         : {plan['access']}/{plan['reason']} {index_ms:.1f} ms "
          f"vs scan baseline {scan_ms:.1f} ms ({scan_ms / index_ms:.1f}x)")

    # 5. estimated vs actual, and the answer invariant, over a query set
    queries = [f"SELECT v FROM t WHERE v >= {value}" for value in
               (0, 10, 500, 2000, 9000, 19000, args.rows - 5)]
    exact = total = 0
    for sql in queries:
        plan = db.run(sql) and db.last_plan
        total += 1
        # A single-predicate index plan serves the whole filter, so what it matched is what it
        # estimated. (rows_examined is deliberately not the yardstick here: a covering path walks
        # the index's keys, not the table's rows.)
        if plan["access"] == "index" and plan["rows_matched"] == plan["estimated_rows"]:
            exact += 1
        fresh = db.run(sql)[1]
        remembered_rows = db.run(sql)[1]
        if fresh != remembered_rows:
            print(f"  ANSWER CHANGED on repeat: {sql}")
            return 1
    print(f"  estimate vs actual         : {exact}/{total} index plans exact "
          f"(rows_matched == estimated_rows)")
    print(f"  answers                    : identical on every repeat ({total} statements)")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
