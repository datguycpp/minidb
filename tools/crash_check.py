#!/usr/bin/env python3
"""Crash evidence for milestone 7.

Every scenario runs in a real subprocess that dies with `os._exit(9)` at a chosen point, then the
database is reopened and inspected. Nothing here simulates a crash with an exception — the
process is actually killed mid-write.

Run: python3 tools/crash_check.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SETUP = """
import sys
sys.path.insert(0, {root!r})
from minidb.engine import Database
db = Database({path!r})
db.run('CREATE TABLE t (a INTEGER)')
{extra}
"""


def run_child(script, crash_point, crash_commit):
    env = {**os.environ, "PYTHONPATH": ROOT,
           "MINIDB_CRASH_POINT": crash_point, "MINIDB_CRASH_COMMIT": str(crash_commit)}
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, timeout=60)


def rows(path):
    sys.path.insert(0, ROOT)
    from minidb.engine import Database
    db = Database(path)
    try:
        return db.run("SELECT * FROM t")[1]
    finally:
        db.close()


def main():
    tmp = tempfile.mkdtemp(prefix="minidb-crash-")
    failures = 0

    scenarios = [
        ("kill after WAL fsync, before page apply", "after_wal", 3,
         "db.run('INSERT INTO t VALUES (42)')", [(42,)]),
        ("kill during page apply", "mid_apply", 3,
         "db.run('INSERT INTO t VALUES (7)')", [(7,)]),
    ]

    for label, point, commit, extra, expected in scenarios:
        path = os.path.join(tmp, f"{point}.db")
        script = SETUP.format(root=ROOT, path=path, extra=extra)
        result = run_child(script, point, commit)
        got = rows(path)
        ok = result.returncode == 9 and got == expected
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        print(f"      child exit={result.returncode}  reopened rows={got}  expected={expected}")

    # An open transaction must vanish entirely when the process dies.
    path = os.path.join(tmp, "tx.db")
    script = SETUP.format(root=ROOT, path=path, extra="db.run('INSERT INTO t VALUES (1)')") + """
db.run('BEGIN')
db.run('INSERT INTO t VALUES (2)')
import os
os._exit(9)
"""
    result = run_child(script, "", 0)
    got = rows(path)
    ok = result.returncode == 9 and got == [(1,)]
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  open transaction lost, committed row kept")
    print(f"      child exit={result.returncode}  reopened rows={got}  expected=[(1,)]")

    # Ten separate crashes in a row: every committed row must survive all of them.
    path = os.path.join(tmp, "repeat.db")
    sys.path.insert(0, ROOT)
    from minidb.engine import Database
    db = Database(path)
    db.run("CREATE TABLE t (a INTEGER)")
    db.close()
    for n in range(1, 11):
        script = f"""
import sys
sys.path.insert(0, {ROOT!r})
from minidb.engine import Database
db = Database({path!r})
db.run('INSERT INTO t VALUES ({n})')
import os
os._exit(9)
"""
        run_child(script, "after_wal", 2)
    got = rows(path)
    expected = [(n,) for n in range(1, 11)]
    ok = got == expected
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  ten consecutive crashes, every committed row kept")
    print(f"      reopened rows={len(got)} expected={len(expected)}")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\ncrash scenarios passing: {4 - failures}/4")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
