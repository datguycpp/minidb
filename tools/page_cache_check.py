#!/usr/bin/env python3
"""Page-cache evidence (milestone 18).

Builds a disk database, then times repeated full scans and indexed lookups with the cache on and
off. Both runs use the same code path; the only difference is MINIDB_CACHE_PAGES, so the number is
an A/B on one binary rather than a comparison of two checkouts.

    python3 tools/page_cache_check.py --rows 20000 --scans 40 --lookups 4000
"""

import argparse
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def build(path, rows):
    from minidb.engine import Database
    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER, v INTEGER)")
    db.run("BEGIN")
    for start in range(1, rows + 1, 500):
        block = ", ".join(f"({i}, {i % 97})" for i in range(start, min(start + 500, rows + 1)))
        db.run(f"INSERT INTO t VALUES {block}")
    db.run("COMMIT")
    db.run("CREATE INDEX idx_v ON t (v)")
    db.close()


def timed(path, scans, lookups):
    from minidb.engine import Database
    db = Database(path)
    started = time.perf_counter()
    total = 0
    for _ in range(scans):
        total += db.run("SELECT count(*) FROM t")[1][0][0]
    scan_time = time.perf_counter() - started
    started = time.perf_counter()
    found = 0
    for i in range(lookups):
        found += len(db.run(f"SELECT id FROM t WHERE v = {i % 97}")[1])
    lookup_time = time.perf_counter() - started
    db.close()
    return total, scan_time, found, lookup_time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--scans", type=int, default=40)
    parser.add_argument("--lookups", type=int, default=4000)
    parser.add_argument("--cache-pages", type=int, default=None)
    args = parser.parse_args()
    if args.cache_pages is not None:
        os.environ["MINIDB_CACHE_PAGES"] = str(args.cache_pages)

    path = os.path.join(tempfile.mkdtemp(), "cache.db")
    build(path, args.rows)
    size = os.path.getsize(path)
    rows_seen, scan_time, lookups_found, lookup_time = timed(path, args.scans, args.lookups)
    print(f"cache_pages={os.environ.get('MINIDB_CACHE_PAGES', '512')} rows={args.rows} "
          f"file_bytes={size}")
    print(f"  scans: {args.scans} in {scan_time:.3f}s -> {args.scans / scan_time:.2f} scans/s")
    print(f"  lookups: {args.lookups} in {lookup_time:.3f}s -> "
          f"{args.lookups / lookup_time:.0f} lookups/s")


if __name__ == "__main__":
    main()
