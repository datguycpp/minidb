#!/usr/bin/env python3
"""Independent reference check: run the corpus against a second SQLite build.

The corpus's expectations are computed at run time from the pinned CLI, so changing the binary
changes the oracle. This tool runs every case three ways — pinned reference, alternate reference,
MiniDB — and classifies what moves:

  reference-changed   the two SQLite builds disagree (the oracle itself moved)
    MiniDB == pinned  the corpus verdict stands; the difference is a version artifact
    MiniDB == alternate  MiniDB happens to match the older/newer build
    MiniDB == neither  MiniDB matches no build: a real finding, not a version artifact
  reference-stable    both builds agree, so the case's verdict is version-independent

Usage:
  python3 tools/reference_check.py --alternate /path/to/sqlite3 [--limit N]
"""

import argparse
import collections
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402


def version_of(binary):
    out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                         check=False)
    return out.stdout.split()[0] if out.returncode == 0 else "unknown"


def brief(result, width=110):
    text = str(result)
    return text if len(text) <= width else text[:width] + "..."


def classify(pinned, alternate, engine):
    if pinned == alternate:
        return "reference-stable"
    if engine == pinned:
        return "reference-changed/minidb-matches-pinned"
    if engine == alternate:
        return "reference-changed/minidb-matches-alternate"
    return "reference-changed/minidb-matches-neither"


def why(pinned, alternate):
    """A short mechanical reason, so the report says what changed rather than only that it did."""
    if isinstance(pinned, dict) and isinstance(alternate, dict):
        if "error" in pinned and "error" in alternate:
            a, b = str(pinned["error"]), str(alternate["error"])
            if a.startswith(b) or b.startswith(a):
                return "error text: numeric suffix differs (CLI result-code formatting)"
            return "error text: wording differs between builds"
        if "columns" in pinned and "columns" in alternate:
            if pinned["columns"] != alternate["columns"]:
                return "column headings differ"
            return "row values differ"
    return "shape or values differ"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alternate", required=True, help="path to the other sqlite3 CLI")
    parser.add_argument("--limit", type=int, default=0, help="stop after N changed cases in printout")
    parser.add_argument("--cases", default=os.path.join(ROOT, "tests", "corpus"))
    args = parser.parse_args()

    pinned_bin = "sqlite3"
    pinned_version = version_of(pinned_bin)
    alternate_version = version_of(args.alternate)
    if pinned_version == alternate_version:
        print("REFERENCE CHECK: both binaries report %s — nothing to compare" % pinned_version)
        return 2

    # Multi-phase cases need a reopen directory; the runner normally owns it per case, so this
    # tool creates and clears one itself.
    scratch = tempfile.mkdtemp(prefix="refcheck-")
    bc.REOPEN_TMPDIR = scratch

    # Same file set the behaviour harness scores: behaviour.txt plus every t*.txt tranche.
    # syntax.txt belongs to the syntax harness and is deliberately excluded (it is not a
    # behaviour case, and counting it would make this tool's total disagree with the score line).
    files = sorted(name for name in os.listdir(args.cases)
                   if name.endswith(".txt") and (name == "behavior.txt" or name.startswith("t")))
    buckets = collections.Counter()
    changed = []
    total = 0
    for name in files:
        path = os.path.join(args.cases, name)
        for index, (_directives, phases) in enumerate(bc.load_cases(path), 1):
            total += 1

            def clear():
                for leftover in os.listdir(scratch):
                    os.remove(os.path.join(scratch, leftover))

            # Each run gets a clean reopen directory: a multi-phase case writes sqlite.db, and
            # leaving it behind makes the next build's run fail with "table t already exists".
            clear()
            pinned = bc.run_sqlite(phases)
            bc.SQLITE_BIN = args.alternate
            clear()
            alternate = bc.run_sqlite(phases)
            bc.SQLITE_BIN = pinned_bin
            clear()
            engine = bc.run_minidb(phases)
            verdict = classify(pinned, alternate, engine)
            buckets[verdict] += 1
            if verdict.startswith("reference-changed"):
                changed.append((name, index, phases[-1][-1], verdict,
                                why(pinned, alternate), pinned, alternate))

    shutil.rmtree(scratch, ignore_errors=True)
    print("REFERENCE CHECK  pinned=%s  alternate=%s" % (pinned_version, alternate_version))
    print("  cases: %d" % total)
    for bucket in sorted(buckets):
        print("    %-46s %4d" % (bucket, buckets[bucket]))
    print()

    if not changed:
        print("  no case changes between the two builds — the corpus is version-independent here")
        return 0

    print("  %d case(s) where the oracle itself moved:" % len(changed))
    shown = changed if not args.limit else changed[:args.limit]
    for name, index, statement, verdict, reason, pinned, alternate in shown:
        print()
        print("  %s case %d  [%s]" % (name, index, verdict.split("/")[-1]))
        print("    query   : %s" % statement.replace("\n", " ")[:120])
        print("    why     : %s" % reason)
        print("    pinned  : %s" % brief(pinned))
        print("    alternate: %s" % brief(alternate))
    if len(shown) != len(changed):
        print("\n  ... %d more not shown (--limit)" % (len(changed) - len(shown)))

    real = [item for item in changed if item[3].endswith("matches-neither")]
    print()
    print("  RESULT: %d reference-version difference(s); %d genuine finding(s) where MiniDB matches "
          "neither build" % (len(changed), len(real)))
    return 0 if not real else 1


if __name__ == "__main__":
    sys.exit(main())
