#!/usr/bin/env python3
"""M26 evidence: the lock-conflict wording, compared with the reference's own wording.

Two-handle scenario, not a single-shot invocation: the reference holds a write transaction open
across statements while a second process tries to write. Our engine holds an exclusive lock on a
handle while a second handle tries to open for writing.

    python3 tools/lock_check.py
"""

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database, ExecError  # noqa: E402

SQLITE = "sqlite3"


def hold_reference_lock(path):
    """Start a CLI process that holds a write transaction open across statements."""
    holder = subprocess.Popen([SQLITE, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    holder.stdin.write("BEGIN IMMEDIATE;\nINSERT INTO t VALUES (99);\n")
    holder.stdin.flush()
    time.sleep(0.4)                       # let the writer take its lock
    return holder


def release_reference_lock(holder):
    holder.stdin.write("ROLLBACK;\n")
    holder.stdin.close()
    holder.wait(timeout=5)


def reference_locked_text(path):
    holder = hold_reference_lock(path)
    second = subprocess.run([SQLITE, path, "INSERT INTO t VALUES (100);"],
                            capture_output=True, text=True)
    release_reference_lock(holder)
    return second.stderr.strip().splitlines()[-1] if second.stderr.strip() else ""


def main():
    path = os.path.join(tempfile.mkdtemp(), "lock.db")
    subprocess.run([SQLITE, path, "CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (1);"],
                   check=True, capture_output=True)

    # Reference wording: two CLI processes, one holding a write transaction.
    reference = reference_locked_text(path)
    reference_text = reference.split(": ", 1)[-1] if ": " in reference else reference

    # Our wording: two MiniDB handles, one holding the exclusive lock. A separate file, because
    # the one above is SQLite's format and MiniDB opens that read-only by design.
    ours_path = os.path.join(tempfile.mkdtemp(), "minidb-lock.db")
    setup = Database(ours_path)
    setup.run("CREATE TABLE t (a INTEGER)")
    setup.close()
    os.environ["MINIDB_LOCK_TIMEOUT"] = "0.3"
    writer = Database(ours_path)
    try:
        Database(ours_path)
        ours = "no conflict raised"
    except ExecError as exc:
        ours = str(exc)
    writer.close()

    readonly_path = os.path.join(tempfile.mkdtemp(), "ro.db")
    subprocess.run([SQLITE, readonly_path, "CREATE TABLE t (a INTEGER);"], check=True,
                   capture_output=True)
    reader = Database(readonly_path, readonly=True)
    try:
        reader.run("INSERT INTO t VALUES (1)")
        refusal = "no refusal raised"
    except ExecError as exc:
        refusal = str(exc)
    reader.close()

    ok = reference_text == ours
    print(f"LOCK reference: {reference_text!r}")
    print(f"LOCK minidb   : {ours!r}   {'MATCH' if ok else 'MISMATCH'}")
    print(f"LOCK readonly refusal: {refusal!r}")
    print(f"M26: {'1/1' if ok else '0/1'} wording matches  reference: SQLite 3.54.0")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
