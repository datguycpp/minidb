#!/usr/bin/env python3
"""Plan-level evidence for the M4/M5 planner.

The conformance corpus compares *results*; an index that is created and ignored scores the same
as one that is used, and two candidate indexes produce identical rows whichever one wins. So the
planner's decisions are evidenced here: access path, reason, estimates for every candidate, rows
examined, and wall clock.

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


def run_case(db, label, sql):
    start = time.perf_counter()
    _, rows = db.run(sql)
    elapsed = (time.perf_counter() - start) * 1000
    plan = db.last_plan or {}
    candidates = ", ".join(f"{c['name']}={c['estimated_rows']}" for c in plan.get("candidates", []))
    print(f"{label:<28} access={plan.get('access'):<6} index={str(plan.get('index')):<10} "
          f"reason={plan.get('reason'):<20} examined={plan.get('rows_examined'):<6} "
          f"est={plan.get('estimated_rows'):<6} {elapsed:7.2f} ms")
    if candidates:
        print(f"{'':28} candidates: {candidates}")
    print(f"{'':28} sql: {sql}")
    print(f"{'':28} rows: {rows[:2]}{'...' if len(rows) > 2 else ''}")
    return plan, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=2000)
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "plans.db"))
    db.run("CREATE TABLE sales (id INTEGER, region TEXT, amount INTEGER, active INTEGER)")

    start = time.perf_counter()
    for i in range(1, args.rows + 1):
        region = ["north", "south", "east", "west"][i % 4]
        db.run(f"INSERT INTO sales VALUES ({i}, '{region}', {i * 3}, 1)")
    print(f"loaded {args.rows} rows in {(time.perf_counter() - start):.2f}s "
          f"(four regions, so region is deliberately unselective)\n")

    query = "SELECT amount FROM sales WHERE id = 137"
    full_plan, full_rows = run_case(db, "1. no index yet", query)

    db.run("CREATE INDEX idx_id ON sales (id)")
    index_plan, index_rows = run_case(db, "2. after CREATE INDEX", query)

    db.run("CREATE INDEX idx_region ON sales (region)")
    db.run("CREATE INDEX idx_amount ON sales (amount)")
    multi_plan, _ = run_case(db, "3. two candidates",
                             "SELECT id FROM sales WHERE region = 'north' AND amount = 408")

    db.run("CREATE INDEX idx_active ON sales (active)")
    run_case(db, "4. unselective index", "SELECT id FROM sales WHERE active = 1")

    run_case(db, "5. no eligible index", "SELECT id FROM sales WHERE id > 0")

    print()
    estimates = {c["name"]: c["estimated_rows"] for c in multi_plan.get("candidates", [])}
    checks = [
        ("full scan before any index", full_plan.get("access") == "full"),
        ("index chosen after CREATE INDEX", index_plan.get("access") == "index"),
        ("result identical with index", full_rows == index_rows),
        ("fewer rows examined with index",
         index_plan.get("rows_examined", 10**9) < full_plan.get("rows_examined", 0)),
        ("multi-candidate picks smallest estimate",
         multi_plan.get("index") == "idx_amount"
         and estimates == {"idx_region": args.rows // 4, "idx_amount": 1}),
    ]
    for label, ok in checks:
        print(f"{label:<40} {'PASS' if ok else 'FAIL'}")

    db.close()
    tmp.cleanup()
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
