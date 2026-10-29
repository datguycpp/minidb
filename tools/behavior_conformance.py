#!/usr/bin/env python3
"""Behavioural conformance against real SQLite (milestone 2).

Syntax conformance asked "does it parse"; this asks "does it produce the same answer". Each
case in tests/corpus/behavior.txt runs against the pinned `sqlite3` CLI and against MiniDB, and
the case passes only when both agree — on rows and column names when SQLite succeeds, or on
the error message when SQLite fails.

Normalisation is deliberately minimal and lives here, not in the engine: SQLite's JSON output
already gives typed values, so the only rewrite is stripping the CLI's error framing.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database, ExecError  # noqa: E402
from minidb.sql.parser import ParseError, parse  # noqa: E402

CORPUS = os.path.join(ROOT, "tests", "corpus", "behavior.txt")
PINNED = "3.54.0"

# The CLI frames errors differently per class: "Error: near line 1: ...", "Parse error: ...",
# "Runtime error near line 2: ...". The framing is the tool's, not SQLite's, so it is stripped.
ERROR_FRAMING = re.compile(r"^(?:Parse |Runtime )?[Ee]rror(?:[:]? near line \d+)?[:]\s*")


def sqlite_version():
    out = subprocess.run(["sqlite3", "--version"], capture_output=True, text=True, check=True)
    return out.stdout.split()[0]


def clean_error(text):
    """Strip the CLI's line-number framing; keep SQLite's actual complaint."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        line = ERROR_FRAMING.sub("", line)
        if line:
            return line
    return (text or "").strip()


def run_sqlite(statements):
    script = ";\n".join(statements) + ";\n"
    proc = subprocess.run(
        ["sqlite3", "-json", ":memory:"],
        input=script, capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0 or proc.stderr.strip():
        return {"error": clean_error(proc.stderr)}
    body = proc.stdout.strip()
    rows = json.loads(body) if body else []
    columns = list(rows[0].keys()) if rows else []
    return {"columns": columns, "rows": rows}


def run_minidb(statements):
    db = Database()
    try:
        result = ([], [])
        for statement in statements:
            result = db.execute(parse(statement))
        columns, rows = result
        keys = list(columns)
        return {"columns": keys, "rows": [dict(zip(keys, row)) for row in rows]}
    except (ExecError, ParseError) as exc:
        return {"error": str(exc)}


def load_cases(path):
    cases, current = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                if current:
                    cases.append(current)
                    current = []
                continue
            if stripped.startswith("--"):
                continue
            current.append(stripped.rstrip(";").strip())
    if current:
        cases.append(current)
    return cases


def compare(expected, actual):
    if "error" in expected:
        if "error" not in actual:
            return False, f"SQLite errored ({expected['error']!r}), MiniDB returned rows"
        return expected["error"] == actual["error"], f"{expected['error']!r} vs {actual['error']!r}"
    if "error" in actual:
        return False, f"SQLite returned rows, MiniDB errored: {actual['error']!r}"
    if expected["columns"] and actual["columns"] and expected["columns"] != actual["columns"]:
        return False, f"columns {expected['columns']} vs {actual['columns']}"
    return expected["rows"] == actual["rows"], f"{expected['rows']} vs {actual['rows']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if shutil.which("sqlite3") is None:
        print("reference binary 'sqlite3' not found on PATH", file=sys.stderr)
        return 2

    version = sqlite_version()
    cases = load_cases(CORPUS)
    results = []
    for index, statements in enumerate(cases, 1):
        expected = run_sqlite(statements)
        actual = run_minidb(statements)
        ok, detail = compare(expected, actual)
        results.append({"case": index, "statements": statements, "pass": ok, "detail": detail})

    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    pct = 100.0 * passed / total if total else 0.0

    if args.json:
        print(json.dumps({"passed": passed, "total": total, "reference": version, "results": results}, indent=2))
        return 0 if passed == total else 1

    for r in results:
        if args.verbose or not r["pass"]:
            tag = "ok  " if r["pass"] else "FAIL"
            print(f"{tag} case {r['case']}: {r['statements'][-1]}")
            if not r["pass"]:
                print(f"     {r['detail']}")

    print(f"\nBEHAVIOUR CONFORMANCE: {passed}/{total} ({pct:.1f}%)")
    print(f"reference: SQLite {version}" + ("" if version == PINNED else f"  *** PINNED IS {PINNED} ***"))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
