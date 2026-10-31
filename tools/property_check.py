#!/usr/bin/env python3
"""M28 property-based checks: the fuzzer's generators turned into properties.

Five properties from docs/tier4-prework.md: round-trip, delete-to-empty/reinsert, crash consistency,
equivalent forms, and VACUUM invariance. Output shape is the spec's:

    PROPERTIES seed=<s> cases=<n> failures=<n>

Every failure prints the schema, data and query that replays it.

    python3 tools/property_check.py --cases 200 --seed 7
"""

import argparse
import os
import random
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
import crash_torture  # noqa: E402
from minidb.engine import Database  # noqa: E402


def generate(rng, rows=12):
    """A small typed table with NULLs, text and reals — the shapes affinity turns on."""
    data = []
    for i in range(1, rows + 1):
        a = "NULL" if rng.random() < 0.2 else str(rng.randint(0, 9))
        b = "NULL" if rng.random() < 0.2 else f"'{rng.choice('abcde')}'"
        c = "NULL" if rng.random() < 0.2 else f"{rng.randint(0, 50) / 2}"
        data.append(f"({i}, {a}, {b}, {c})")
    return "CREATE TABLE t (id INTEGER, a INTEGER, b TEXT, c REAL)", data


def property_roundtrip(rng, path):
    schema, data = generate(rng)
    db = Database(path)
    db.run(schema)
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    stored = db.run("SELECT id, a, b, c, typeof(a), typeof(b), typeof(c) FROM t ORDER BY id")[1]
    db.close()
    again = Database(path)
    reread = again.run("SELECT id, a, b, c, typeof(a), typeof(b), typeof(c) FROM t ORDER BY id")[1]
    again.close()
    return None if stored == reread else f"round-trip mismatch: {stored[:2]} vs {reread[:2]}"


def property_delete_reinsert(rng, path):
    schema, data = generate(rng)
    db = Database(path)
    db.run(schema)
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    before = db.run("SELECT id, a, b, c FROM t ORDER BY id")[1]
    db.run("DELETE FROM t")
    empty = db.run("SELECT count(*) FROM t")[1][0][0]
    if empty != 0:
        db.close()
        return f"delete left {empty} rows"
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    after = db.run("SELECT id, a, b, c FROM t ORDER BY id")[1]
    db.close()
    return None if before == after else "reinsert did not restore the same reads"


def property_crash_consistency(rng, path):
    seed = rng.randint(1, 10_000)
    result = crash_torture.run_iteration_forked(path, seed, rows=10)
    if result.returncode not in (0, 9):
        return f"child exited {result.returncode}"
    return crash_torture.check(path)


EQUIVALENT_FORMS = [
    ("SELECT id FROM t WHERE a = 1 AND b = 'a'",
     "SELECT id FROM t WHERE b = 'a' AND a = 1"),
    ("SELECT id FROM t WHERE NOT (a = 1 AND b = 'a')",
     "SELECT id FROM t WHERE a != 1 OR b != 'a'"),
    ("SELECT id FROM t WHERE a IS NOT NULL",
     "SELECT id FROM t WHERE NOT (a IS NULL)"),
]


def property_equivalent_forms(rng, path):
    schema, data = generate(rng)
    db = Database(path)
    db.run(schema)
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    for left, right in EQUIVALENT_FORMS:
        one = db.run(left)[1]
        two = db.run(right)[1]
        if sorted(one) != sorted(two):
            db.close()
            return f"equivalent forms disagree: {left} vs {right}"
    db.close()
    return None


def property_vacuum_invariance(rng, path):
    schema, data = generate(rng)
    db = Database(path)
    db.run(schema)
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    db.run("DELETE FROM t WHERE id % 3 = 0")
    query = "SELECT count(*), sum(a), min(b), max(c) FROM t"
    before = db.run(query)[1]
    db.run("VACUUM")
    after = db.run(query)[1]
    db.close()
    return None if before == after else f"vacuum changed answers: {before} vs {after}"


PROPERTIES = [property_roundtrip, property_delete_reinsert, property_crash_consistency,
              property_equivalent_forms, property_vacuum_invariance]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    failures = 0
    for case in range(args.cases):
        for prop in PROPERTIES:
            path = os.path.join(tempfile.mkdtemp(), f"{prop.__name__}-{case}.db")
            problem = prop(rng, path)
            if problem:
                failures += 1
                print(f"FAIL {prop.__name__} case={case} seed={args.seed}: {problem}")
    print(f"PROPERTIES seed={args.seed} cases={args.cases} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
