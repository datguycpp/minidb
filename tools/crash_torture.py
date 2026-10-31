#!/usr/bin/env python3
"""M24 crash torture: random crashes, then invariants — never "the file opened".

Per docs/tier4-prework.md: a crash is a fresh process killed at a random point inside the write
path; randomness is a seeded RNG so every failing iteration replays from its log line; and the
check after reopen is invariants, not existence:

  1. rows present form a contiguous committed prefix (no torn state),
  2. the index agrees with a full scan (a lying index is data loss's cousin),
  3. the file reopens and is queryable.

    python3 tools/crash_torture.py --iterations 2000 --rows 20
"""

import argparse
import os
import random
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CHILD = r'''
import os, random, sys
sys.path.insert(0, {root!r})
from minidb.engine import Database
path, seed, rows = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rng = random.Random(seed)
db = Database(path)
db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
db.run("CREATE INDEX idx_v ON t (v)")
crash_after = rng.randint(1, rows)
for i in range(1, rows + 1):
    db.run(f"INSERT INTO t VALUES ({{i}}, {{i % 5}})")
    if i >= crash_after:
        os._exit(9)          # die without closing: the WAL is the only record
db.close()
'''


def run_iteration(path, seed, rows):
    script = CHILD.format(root=ROOT)
    result = subprocess.run([sys.executable, "-c", script, path, str(seed), str(rows)],
                            capture_output=True, text=True)
    return result


SITES = ["wal_before_write", "wal_after_write", "wal_after_flush", "wal_after_fsync",
         "db_before_apply", "db_mid_apply", "db_after_first_page", "db_after_flush",
         "db_after_fsync", "truncate_before", "truncate_after", "db_torn_page",
         "vacuum_before_replace", "vacuum_mid_replace", "vacuum_after_replace"]


def run_iteration_forked(path, seed, rows, site=None, commit=1):
    """Same crash without paying interpreter start-up per iteration.

    A 10,000-iteration gate run at ~0.4 s per exec is over an hour; forking the already-imported
    engine makes the same crash shape affordable. The child still dies with os._exit(9) mid-write.
    """
    from minidb.engine import Database
    pid = os.fork()
    if pid == 0:
        try:
            if site:
                os.environ["MINIDB_CRASH_POINT"] = site
                os.environ["MINIDB_CRASH_COMMIT"] = str(commit)
            rng = random.Random(seed)
            db = Database(path)
            db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
            db.run("CREATE INDEX idx_v ON t (v)")
            crash_after = rng.randint(1, rows) if not site else rows + 1
            for i in range(1, rows + 1):
                db.run(f"INSERT INTO t VALUES ({i}, {i % 5})")
                if i >= crash_after:
                    os._exit(9)
            if site and site.startswith("vacuum_"):
                # VACUUM's file-replace path is only reachable from a VACUUM.
                db.run("DELETE FROM t WHERE id > 5")
                db.run("VACUUM")
            db.close()
        except BaseException:
            os._exit(70)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return type("Result", (), {"returncode": code, "stderr": ""})()


def check_database(db):
    """Invariants over an open database: contiguous prefix, and indexes that tell the truth.

    A crash during the CREATE TABLE commit leaves a database with no table at all; that is a
    consistent state, not a failure, so it passes with zero rows.
    """
    if "t" not in db.tables:
        return None
    columns, rows = db.run("SELECT id FROM t ORDER BY id")
    ids = [row[0] for row in rows]
    if ids != list(range(1, len(ids) + 1)):
        return f"rows are not a contiguous committed prefix: {ids[:5]}…{ids[-3:]}"
    table = db.tables["t"]
    rows = list(db._rows(table))         # direct table scan: the planner cannot lie here
    for index in table.indexes.values():
        for key, bucket in index.map.items():
            expected = sum(1 for row in rows if index.key_for(row) == key)
            if len(bucket) != expected:
                return f"index {index.name} disagrees at {key}: {len(bucket)} vs {expected}"
        indexed = sum(len(bucket) for bucket in index.map.values())
        if indexed != len(rows):
            return f"index {index.name} holds {indexed} rows, table has {len(rows)}"
    return None


def check(path):
    """Invariants after recovery."""
    from minidb.engine import Database
    db = Database(path)
    try:
        return check_database(db)
    finally:
        db.close()


def sweep(args):
    """One row per crash site: iterations, how many actually died at that site, and clean reopens."""
    rows_out = []
    total_failures = 0
    for site in SITES:
        died = 0
        failures = []
        for offset in range(args.iterations):
            seed = args.start_seed + offset
            path = os.path.join(tempfile.mkdtemp(), f"sweep-{site}-{seed}.db")
            result = run_iteration_forked(path, seed, args.rows, site=site, commit=1)
            if result.returncode == 9:
                died += 1
            elif result.returncode != 0:
                failures.append((seed, f"child exit {result.returncode}"))
                continue
            problem = check(path)
            if problem:
                failures.append((seed, problem))
        clean = args.iterations - len(failures)
        total_failures += len(failures)
        rows_out.append((site, args.iterations, died, clean, failures[:2]))
    print(f"CRASH SITE SWEEP: rows={args.rows} iterations_per_site={args.iterations} "
          f"seeds={args.start_seed}..{args.start_seed + args.iterations - 1}")
    print(f"  {'site':<20} {'iters':>6} {'died_here':>9} {'clean_reopen':>13}")
    for site, iterations, died, clean, failures in rows_out:
        note = "" if died else "   <- site never reached"
        print(f"  {site:<20} {iterations:>6} {died:>9} {clean:>13}{note}")
        for seed, problem in failures:
            print(f"      FAIL seed={seed}: {problem}")
    sites_hit = sum(1 for _, _, died, _, _ in rows_out if died)
    print(f"COVERAGE: {sites_hit}/{len(SITES)} sites reached, "
          f"{sum(r[2] for r in rows_out)} crashes injected, {total_failures} invariant failures")
    return 1 if total_failures else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--rows", type=int, default=20)
    parser.add_argument("--start-seed", type=int, default=1)
    parser.add_argument("--sweep", action="store_true",
                        help="run every crash site and print the coverage table")
    parser.add_argument("--fork", action="store_true",
                        help="fork per iteration instead of exec (much faster at 10k)")
    args = parser.parse_args()

    if args.sweep:
        return sweep(args)

    failures = []
    for offset in range(args.iterations):
        seed = args.start_seed + offset
        path = os.path.join(tempfile.mkdtemp(), f"crash-{seed}.db")
        result = (run_iteration_forked(path, seed, args.rows) if args.fork
                  else run_iteration(path, seed, args.rows))
        if result.returncode not in (0, 9):
            failures.append((seed, f"child exit {result.returncode}: {result.stderr.strip()[:200]}"))
            continue
        problem = check(path)
        if problem:
            failures.append((seed, problem))
    clean = args.iterations - len(failures)
    print(f"CRASH TORTURE: {clean}/{args.iterations} clean  rows={args.rows} "
          f"seeds={args.start_seed}..{args.start_seed + args.iterations - 1}")
    for seed, problem in failures[:10]:
        print(f"  FAIL seed={seed}: {problem}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
