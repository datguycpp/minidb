#!/usr/bin/env python3
"""M28 property-based checks: the fuzzer's generators turned into properties.

Five properties from docs/tier4-prework.md — round-trip, delete-to-empty/reinsert, crash
consistency, equivalent forms, VACUUM invariance — over generated schemas whose shape is declared in
the header, per the fuzz spec's rule that a rate without a declared dialect is unverifiable.

    PROPERTIES seed=<s> cases=<n> failures=<n>
    schema features: <list actually emitted>

Failures print the schema, data and query that replay them.

    python3 tools/property_check.py --cases 250 --seeds 1,2,3,4
"""

import argparse
import os
import random
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import crash_torture  # noqa: E402
from minidb.engine import Database  # noqa: E402

EQUIVALENT_FORMS = [
    ("SELECT id FROM t WHERE a = 1 AND b = 'a'",
     "SELECT id FROM t WHERE b = 'a' AND a = 1"),
    ("SELECT id FROM t WHERE NOT (a = 1 AND b = 'a')",
     "SELECT id FROM t WHERE a != 1 OR b != 'a'"),
    ("SELECT id FROM t WHERE a IS NOT NULL",
     "SELECT id FROM t WHERE NOT (a IS NULL)"),
    ("SELECT id FROM t WHERE a IN (1, 2, 3)",
     "SELECT id FROM t WHERE a = 1 OR a = 2 OR a = 3"),
    ("SELECT DISTINCT a FROM t",
     "SELECT a FROM t GROUP BY a"),
    ("SELECT t.id FROM t LEFT JOIN u ON t.id = u.id WHERE u.id IS NOT NULL",
     "SELECT t.id FROM t JOIN u ON t.id = u.id"),
    ("SELECT count(*) FROM t WHERE a IS NULL",
     "SELECT count(*) - count(a) FROM t"),
    ("SELECT id FROM t WHERE a NOT IN (SELECT a FROM u WHERE a IS NOT NULL)",
     "SELECT id FROM t WHERE a NOT IN (SELECT a FROM u) OR a IN (SELECT a FROM u WHERE a IS NULL)"),
]

# The last pair is only equivalent when u has no NULL a; generated data inserts none, and the
# generator is part of the replay triple, so the property stays checkable.


def generate(rng, rows=12):
    """Schema + data whose shape is reported in the run header."""
    features = {"affinity-mix", "nulls"}
    nocase = rng.random() < 0.5
    if nocase:
        features.add("nocase")
    blob = rng.random() < 0.4
    if blob:
        features.add("blob")
    statements = [f"CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, "
                  f"b TEXT{' COLLATE NOCASE' if nocase else ''}, c REAL, d BLOB, e NUMERIC, f TEXT)"]
    if rng.random() < 0.5:
        features.add("index")
        statements.append("CREATE INDEX idx_a ON t (a)")
    if rng.random() < 0.3:
        features.add("composite-index")
        statements.append("CREATE INDEX idx_ab ON t (a, b)")
    if rng.random() < 0.3:
        features.add("trigger")
        statements.append("CREATE TABLE log (id INTEGER)")
        statements.append("CREATE TRIGGER tr AFTER INSERT ON t BEGIN "
                          "INSERT INTO log VALUES (NEW.id); END")
    if rng.random() < 0.3:
        features.add("view")
        statements.append("CREATE VIEW v AS SELECT id, a FROM t WHERE a IS NOT NULL")
    statements.append("CREATE TABLE u (id INTEGER, a INTEGER)")
    statements.append("INSERT INTO u VALUES " + ", ".join(
        f"({i}, {rng.randint(0, 9)})" for i in range(1, 8)))
    data = []
    for i in range(1, rows + 1):
        a = "NULL" if rng.random() < 0.25 else str(rng.randint(0, 9))
        b = "NULL" if rng.random() < 0.25 else f"'{rng.choice('abcde')}'"
        c = "NULL" if rng.random() < 0.25 else f"{rng.randint(0, 50) / 2}"
        d = "NULL" if not blob or rng.random() < 0.5 else f"X'{rng.randrange(256):02X}'"
        e = rng.choice(["'3.0'", "3", "NULL", "'12abc'"])
        f = "NULL" if rng.random() < 0.2 else f"'{rng.choice('xyz')}'"
        data.append(f"({i}, {a}, {b}, {c}, {d}, {e}, {f})")
    return statements, data, features


def property_roundtrip(rng, path, statements, data):
    db = Database(path)
    for statement in statements:
        db.run(statement)
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    query = ("SELECT id, a, b, c, hex(d), e, f, typeof(a), typeof(b), typeof(c), typeof(d) "
             "FROM t ORDER BY id")
    stored = db.run(query)[1]
    db.close()
    again = Database(path)
    reread = again.run(query)[1]
    again.close()
    return None if stored == reread else f"round-trip mismatch: {stored[:2]} vs {reread[:2]}"


def property_delete_reinsert(rng, path, statements, data):
    db = Database(path)
    for statement in statements:
        db.run(statement)
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    query = "SELECT id, a, b, c, hex(d), e FROM t ORDER BY id"
    before = db.run(query)[1]
    db.run("DELETE FROM t")
    empty = db.run("SELECT count(*) FROM t")[1][0][0]
    if empty != 0:
        db.close()
        return f"delete left {empty} rows"
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    after = db.run(query)[1]
    db.close()
    return None if before == after else "reinsert did not restore the same reads"


def property_crash_consistency(rng, path, statements, data):
    seed = rng.randint(1, 100_000)
    site = rng.choice(crash_torture.SITES)      # the full site list, not statement kills only
    result = crash_torture.run_iteration_forked(path, seed, rows=10, site=site, commit=1)
    if result.returncode not in (0, 9):
        return f"child exited {result.returncode} at site {site}"
    problem = crash_torture.check(path)
    return None if problem is None else f"[site {site}] {problem}"


def property_equivalent_forms(rng, path, statements, data):
    db = Database(path)
    for statement in statements:
        db.run(statement)
    db.run("INSERT INTO t VALUES " + ", ".join(data))
    for left, right in EQUIVALENT_FORMS:
        one = sorted(db.run(left)[1], key=repr)
        two = sorted(db.run(right)[1], key=repr)
        if one != two:
            db.close()
            return f"equivalent forms disagree: {left} vs {right} ({one[:3]} vs {two[:3]})"
    db.close()
    return None


def property_vacuum_invariance(rng, path, statements, data):
    db = Database(path)
    for statement in statements:
        db.run(statement)
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


def run_seed(seed, cases):
    rng = random.Random(seed)
    failures = 0
    emitted = set()
    for case in range(cases):
        statements, data, features = generate(rng)
        emitted |= features
        for prop in PROPERTIES:
            path = os.path.join(tempfile.mkdtemp(), f"{prop.__name__}-{case}.db")
            problem = prop(rng, path, statements, data)
            if problem:
                failures += 1
                print(f"FAIL {prop.__name__} seed={seed} case={case}: {problem}")
                print(f"     schema: {statements}")
                print(f"     data: {data[:3]}…")
    print(f"PROPERTIES seed={seed} cases={cases} failures={failures}")
    return failures, emitted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument("--seeds", default="1")
    args = parser.parse_args()
    seeds = [int(part) for part in args.seeds.split(",")]

    total_failures = 0
    emitted = set()
    for seed in seeds:
        failures, features = run_seed(seed, args.cases)
        total_failures += failures
        emitted |= features
    print(f"schema features: {', '.join(sorted(emitted))}")
    print(f"PROPERTIES TOTAL cases={args.cases * len(seeds)} seeds={len(seeds)} "
          f"failures={total_failures}")
    return 1 if total_failures else 0


if __name__ == "__main__":
    sys.exit(main())
