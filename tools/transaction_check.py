#!/usr/bin/env python3
"""Transaction evidence (milestone 6).

Prints, for each script, what SQLite does and what MiniDB does side by side — rows or error text.
The corpus gains transaction cases with t1d; until then this is the artifact that shows the
semantics match rather than asserting it.

Run: python3 tools/transaction_check.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402

SCRIPTS = [
    ("rollback discards insert",
     ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (1)",
      "BEGIN", "INSERT INTO t VALUES (2)", "ROLLBACK", "SELECT * FROM t"]),
    ("commit keeps insert",
     ["CREATE TABLE t (a INTEGER)", "BEGIN", "INSERT INTO t VALUES (1)", "COMMIT", "SELECT * FROM t"]),
    ("rollback discards CREATE TABLE",
     ["BEGIN", "CREATE TABLE ghost (x INTEGER)", "ROLLBACK", "SELECT * FROM ghost"]),
    ("rollback leaves prior rows",
     ["CREATE TABLE t (a INTEGER)", "INSERT INTO t VALUES (10), (20)",
      "BEGIN", "INSERT INTO t VALUES (30)", "ROLLBACK", "SELECT * FROM t"]),
    ("commit without begin",
     ["COMMIT"]),
    ("rollback without begin",
     ["ROLLBACK"]),
    ("nested begin",
     ["BEGIN", "BEGIN"]),
    ("begin/end spelling",
     ["CREATE TABLE t (a INTEGER)", "BEGIN TRANSACTION", "INSERT INTO t VALUES (5)",
      "END TRANSACTION", "SELECT * FROM t"]),
]


def describe(result):
    if "error" in result:
        return f"ERROR {result['error']!r}"
    if not result.get("rows"):
        return "no rows"
    return f"{len(result['rows'])} row(s): {result['rows']}"


def main():
    failures = 0
    for label, statements in SCRIPTS:
        expected = bc.run_sqlite([statements])
        actual = bc.run_minidb([statements])
        ok, detail = bc.compare(expected, actual)
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        print(f"      sqlite: {describe(expected)}")
        print(f"      minidb: {describe(actual)}")
        if not ok:
            print(f"      detail: {detail}")

    print()
    print(f"transactions matching SQLite: {len(SCRIPTS) - failures}/{len(SCRIPTS)}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
