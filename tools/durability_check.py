#!/usr/bin/env python3
"""Durability evidence for milestone 3.

The differential corpus runs against an in-memory database and never reopens, so it cannot say
anything about persistence. This script is the evidence that storage works: it writes a real
file, closes it, reopens it in a separate process-level open, and compares row content by hash.

Run: python3 tools/durability_check.py [--rows N] [--keep PATH]
"""

import argparse
import hashlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402
from minidb.storage.pager import PAGE_SIZE  # noqa: E402


def digest(rows):
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()[:16]


def report(label, path, columns, rows):
    pages = os.path.getsize(path) // PAGE_SIZE
    print(f"{label}: rows={len(rows):<5} pages={pages:<4} columns={columns} sha256={digest(rows)}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=500)
    ap.add_argument("--keep", metavar="PATH", help="write to this path instead of a temp dir")
    args = ap.parse_args()

    if args.keep:
        path = args.keep
        if os.path.exists(path):
            os.unlink(path)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory()
        path = os.path.join(cleanup.name, "durability.db")

    expected = [(i, f"row-{i:04d}", i * 1.5, None if i % 3 == 0 else "héllo") for i in range(1, args.rows + 1)]

    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, label TEXT NOT NULL, weight REAL, note TEXT)")
    for row in expected:
        note = "NULL" if row[3] is None else "'" + row[3] + "'"
        db.run(f"INSERT INTO t VALUES ({row[0]}, '{row[1]}', {row[2]}, {note})")
    columns, rows = db.run("SELECT * FROM t")
    report("write  ", path, columns, rows)
    db.close()

    # Fresh open: nothing shared with the writer except the file.
    db = Database(path)
    columns, rows = db.run("SELECT * FROM t")
    report("reopen ", path, columns, rows)
    ok_first = rows == expected
    db.close()

    # Append in a second session, then verify a third open sees both generations.
    db = Database(path)
    db.run(f"INSERT INTO t VALUES ({args.rows + 1}, 'after-reopen', 0.5, 'new')")
    db.close()
    db = Database(path)
    columns, rows = db.run("SELECT * FROM t")
    report("append ", path, columns, rows)
    ok_second = rows == expected + [(args.rows + 1, "after-reopen", 0.5, "new")]
    db.close()

    # A rolled-back transaction must leave nothing behind once the file is reopened.
    db = Database(path)
    db.run("BEGIN")
    db.run(f"INSERT INTO t VALUES ({args.rows + 2}, 'rolled-back', 1.0, 'gone')")
    db.run("ROLLBACK")
    db.close()
    db = Database(path)
    columns, rows = db.run("SELECT * FROM t")
    ok_third = rows == expected + [(args.rows + 1, "after-reopen", 0.5, "new")]
    print(f"rollback: rows={len(rows):<5} (unchanged after rollback+reopen: {'yes' if ok_third else 'no'})")
    db.close()

    print()
    print(f"content identical across close/reopen: {'PASS' if ok_first else 'FAIL'}")
    print(f"row written after reopen persisted:    {'PASS' if ok_second else 'FAIL'}")
    print(f"rolled-back row absent after reopen:   {'PASS' if ok_third else 'FAIL'}")
    if cleanup:
        cleanup.cleanup()
    return 0 if (ok_first and ok_second and ok_third) else 1


if __name__ == "__main__":
    sys.exit(main())
