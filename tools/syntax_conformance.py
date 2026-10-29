#!/usr/bin/env python3
"""Syntax conformance against real SQLite.

Milestone 1's denominator: for every statement in the corpus ask *SQLite's parser* whether
the statement is valid, and ask MiniDB's parser the same question. A case passes when the two
agree; both directions count, because accepting something SQLite rejects is as wrong as
rejecting something it accepts, and only the second kind usually gets noticed.

Two things this harness is careful about, both learned the hard way:

1. **The reference is the pinned CLI binary, not Python's sqlite3 module.** The module on this
   box is 3.53.1 while STATE.md pins 3.54.0; scoring against the wrong build makes every
   number quietly incomparable.
2. **Semantic errors are not syntax errors.** "no such table", "no such column" and "table
   already exists" mean SQLite's *parser* accepted the statement. Only syntax-class failures
   count against the parser, so they are classified explicitly rather than by exit code.

Usage:
    python3 tools/syntax_conformance.py            # mismatches + score line
    python3 tools/syntax_conformance.py --verbose  # one line per case
    python3 tools/syntax_conformance.py --json
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

from minidb.sql.parser import ParseError, parse  # noqa: E402

CORPUS = os.path.join(ROOT, "tests", "corpus", "syntax.txt")
PINNED = "3.54.0"

# A schema so that SELECT/INSERT statements fail (or not) on syntax, not on "no such table".
FIXTURE = "CREATE TABLE IF NOT EXISTS t (id INTEGER, name TEXT);"

SYNTAX_MARKERS = (
    "syntax error",
    "incomplete input",
    "unrecognized token",
    "unterminated",
    "unexpected end",
)


def sqlite_version():
    out = subprocess.run(["sqlite3", "--version"], capture_output=True, text=True, check=True)
    return out.stdout.split()[0]


def sqlite_syntax_ok(statement):
    """True when SQLite's parser accepts the statement (semantic failures still count as ok)."""
    # The fixture would otherwise shadow a corpus statement that creates table `t`, turning a
    # syntax error into "table already exists" and mis-scoring it as syntactically valid.
    needs_fixture = not statement.lstrip().upper().startswith("CREATE")
    script = (FIXTURE + "\n" if needs_fixture else "") + statement + "\n"
    proc = subprocess.run(
        ["sqlite3", "-batch", ":memory:"],
        input=script, capture_output=True, text=True, timeout=10,
    )
    stderr = (proc.stderr or "").strip()
    if proc.returncode == 0 and not stderr:
        return True, None
    lowered = stderr.lower()
    if any(marker in lowered for marker in SYNTAX_MARKERS):
        return False, first_line(stderr)
    return True, first_line(stderr)  # semantic complaint: syntax was fine


def first_line(text):
    """The CLI prefixes complaints with its own framing; drop that, keep the reason."""
    for line in text.splitlines():
        line = re.sub(r"^(Parse |Runtime )?error near line \d+:\s*", "", line.strip(), flags=re.I)
        if line:
            return line
    return text.strip()


def minidb_syntax_ok(statement):
    try:
        parse(statement)
        return True, None
    except ParseError as exc:
        return False, str(exc)


def load_corpus(path):
    cases = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            raw = line.rstrip("\n")
            stripped = raw.lstrip()
            if not stripped or (stripped.startswith("#") and not stripped.startswith("#!")):
                continue
            reject_marker = stripped.startswith("#!")
            statement = raw[2:].strip() if reject_marker else raw.strip()
            cases.append({"line": number, "statement": statement, "reject_marker": reject_marker})
    return cases


def run(cases):
    results = []
    for case in cases:
        sqlite_ok, sqlite_note = sqlite_syntax_ok(case["statement"])
        ours_ok, ours_note = minidb_syntax_ok(case["statement"])
        results.append({
            "line": case["line"],
            "statement": case["statement"],
            "sqlite": sqlite_ok,
            "minidb": ours_ok,
            "pass": sqlite_ok == ours_ok,
            "sqlite_note": sqlite_note,
            "minidb_note": ours_note,
            "marker_matches": case["reject_marker"] == (not sqlite_ok),
        })
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if shutil.which("sqlite3") is None:
        print("reference binary 'sqlite3' not found on PATH", file=sys.stderr)
        return 2

    version = sqlite_version()
    cases = load_corpus(CORPUS)
    results = run(cases)
    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    pct = 100.0 * passed / total if total else 0.0

    if args.json:
        print(json.dumps({"passed": passed, "total": total, "reference": version, "results": results}, indent=2))
        return 0 if passed == total else 1

    if args.verbose:
        for r in results:
            print(f"{'ok  ' if r['pass'] else 'FAIL'} L{r['line']:<3} "
                  f"sqlite={str(r['sqlite']):<5} minidb={str(r['minidb']):<5} {r['statement']}")
    else:
        for r in results:
            if r["pass"]:
                continue
            direction = "we accept, SQLite rejects" if r["minidb"] else "we reject, SQLite accepts"
            print(f"FAIL L{r['line']}: {direction}")
            print(f"     {r['statement']}")
            print(f"     sqlite: {r['sqlite_note'] or 'accepted'}")
            print(f"     minidb: {r['minidb_note'] or 'accepted'}")

    print(f"\nSYNTAX CONFORMANCE: {passed}/{total} ({pct:.1f}%)")
    print(f"reference: SQLite {version}" + ("" if version == PINNED else f"  *** PINNED IS {PINNED} ***"))
    bad = [r for r in results if not r["marker_matches"]]
    if bad:
        print("corpus markers disagree with SQLite on lines: " + ", ".join(str(r["line"]) for r in bad))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
