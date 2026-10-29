#!/usr/bin/env python3
"""Plan-level evidence for indexes (milestone 4).

The conformance corpus compares *results*, so it cannot tell whether an index was used — an
index that is created and then ignored scores exactly the same. EXPLAIN output is milestone 32,
so until then this script is the artifact: it prints the access path actually taken and the
number of rows examined, plus wall-clock timings, with and without an index.

Run: python3 tools/plan_check.py [--rows N]
"""

import argparse
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402


def timed(db, sql):
    start = time.perf_counter()
    columns, rows = db.run(sql)
    elapsed = (time.perf_counter() - start) * 1000
    plan = db.last_plan or {}
    return columns, rows, elapsed, plan


def show(label, sql, columns, rows, elapsed, plan):
    access = plan.get("access", "?")
    index = plan.get("index") or "-"
    print(f"{label:<26} access={access:<6} index={index:<8} examined={plan.get('rows_examined', '?'):<6} "
          f"returned={len(rows):<3} {elapsed:7.2f} ms   {sql}")
    print(f"{'':26} columns={columns} rows={rows[:3]}{'...' if len(rows) > 3 else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=2000)
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "plans.db"))
    db.run("CREATE TABLE sales (id INTEGER, region TEXT, amount INTEGER)")

    start = time.perf_counter()
    for i in range(1, args.rows + 1):
        region = ["north", "south", "east", "west"][i % 4]
        db.run(f"INSERT INTO sales VALUES ({i}, '{region}', {i * 3})")
    print(f"loaded {args.rows} rows in {(time.perf_counter() - start):.2f}s\n")

    query = "SELECT amount FROM sales WHERE id = 137"

    show("before index", query, *timed(db, query))
    db.run("CREATE INDEX idx_id ON sales (id)")
    show("after CREATE INDEX", query, *timed(db, query))

    unindexed = "SELECT amount FROM sales WHERE amount = 411"
    show("no index for column", unindexed, *timed(db, unindexed))

    print()
    before = timed(db, query)
    after = timed(db, query)
    print(f"rows examined with index: {after[3]['rows_examined']} (full scan would be {args.rows})")
    print(f"index actually chosen:    {'PASS' if after[3].get('access') == 'index' else 'FAIL'}")
    print(f"result identical:         {'PASS' if before[1] == after[1] else 'FAIL'}")

    db.close()
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
