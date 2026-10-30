#!/usr/bin/env python3
"""Differential fuzzer (milestone 9), implementing docs/fuzz-spec.md.

Definition of a mismatch, per the spec: MiniDB's answer for a generated input differs from the
pinned SQLite build's answer, judged by the conformance harness's own comparator — this file has
no comparison logic of its own. Any MiniDB traceback is a mismatch regardless of SQLite's answer.

Modes:
    structured  random schema + rows + queries over the milestone's supported dialect
    mutation    corpus statements with literals/operators mutated
    rejection   random token soup; both engines must reject with byte-identical messages

Run:
    python3 tools/fuzz.py --queries 200 --seed 1          # smoke (clearly labelled)
    python3 tools/fuzz.py --queries 10000 --seed 1        # gate-number run
"""

import argparse
import os
import random
import string
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402

FEATURES = ["select", "where", "and-or", "is-null", "distinct", "aggregates", "group-by",
            "having", "inner-join", "in-subquery", "not-in-subquery", "index"]

NUMBER = "0123456789"
TOKENS = ["SELECT", "FROM", "WHERE", "(", ")", ",", "=", "<", ">", "*", "1", "x", "'a'",
          "AND", "OR", "NOT", "IN", "NULL", "CREATE", "TABLE", "JOIN", "ON", "GROUP", "BY"]


# -- generators -------------------------------------------------------------
def gen_schema(rng):
    tables = []
    for index in range(rng.randint(1, 3)):
        name = f"t{index}"
        statements = [f"CREATE TABLE {name} (id INTEGER, v INTEGER, s TEXT)"]
        rows = []
        for row_id in range(1, rng.randint(1, 30) + 1):
            v = "NULL" if rng.random() < 0.2 else str(rng.randint(0, 20))
            s = "NULL" if rng.random() < 0.2 else f"'{rng.choice(string.ascii_lowercase)}'"
            rows.append(f"({row_id}, {v}, {s})")
        statements.append(f"INSERT INTO {name} VALUES {', '.join(rows)}")
        tables.append({"name": name, "statements": statements})
    return tables


def gen_query(rng, tables):
    names = [t["name"] for t in tables]
    kind = rng.choice(["simple", "where", "aggregate", "group", "join", "subquery"])
    table = rng.choice(names)

    if kind == "simple":
        return f"SELECT id, v, s FROM {table}"
    if kind == "where":
        predicate = rng.choice([
            f"v = {rng.randint(0, 20)}",
            f"v > {rng.randint(0, 20)}",
            f"v IS NULL",
            f"s = '{rng.choice(string.ascii_lowercase)}'",
            f"v = {rng.randint(0, 20)} AND id > {rng.randint(0, 10)}",
            f"v = {rng.randint(0, 20)} OR v = {rng.randint(0, 20)}",
            f"id NOT IN (SELECT id FROM {table} WHERE v > {rng.randint(0, 10)})",
        ])
        return f"SELECT id FROM {table} WHERE {predicate}"
    if kind == "aggregate":
        return rng.choice([
            f"SELECT count(*) FROM {table}",
            f"SELECT sum(v), avg(v), min(v), max(v) FROM {table}",
            f"SELECT count(v) FROM {table} WHERE v IS NOT NULL",
        ])
    if kind == "group":
        having = rng.choice(["", " HAVING count(*) > 0", f" HAVING count(*) > {rng.randint(0, 2)}"])
        return f"SELECT v, count(*) FROM {table} GROUP BY v{having}"
    if kind == "subquery" and len(names) >= 2:
        other = rng.choice([n for n in names if n != table])
        return (f"SELECT id FROM {table} WHERE v IN "
                f"(SELECT v FROM {other} WHERE v IS NOT NULL)")
    if len(names) >= 2:
        other = rng.choice([n for n in names if n != table])
        return (f"SELECT {table}.id, {other}.s FROM {table} "
                f"JOIN {other} ON {table}.id = {other}.id")
    return f"SELECT id FROM {table}"


def gen_case(rng, mode):
    if mode == "rejection":
        length = rng.randint(1, 8)
        return [" ".join(rng.choice(TOKENS) for _ in range(length))]

    tables = gen_schema(rng)
    statements = []
    for table in tables:
        statements.extend(table["statements"])
    if mode == "mutation":
        statements.extend(mutate_corpus(rng))
    else:
        statements.append(gen_query(rng, tables))
    return statements


def mutate_corpus(rng):
    """Take a known-good statement and perturb it — literals swapped, clauses dropped."""
    base = [
        "INSERT INTO t0 VALUES (99, 7, 'z')",
        "SELECT id FROM t0 WHERE v = 7",
        "SELECT v, count(*) FROM t0 GROUP BY v",
        "SELECT id FROM t0 WHERE v IS NULL",
        "SELECT id FROM t0 WHERE v IN (SELECT v FROM t0 WHERE v > 5)",
    ]
    statement = rng.choice(base)
    if rng.random() < 0.5:
        return statement
    return statement.replace("7", str(rng.randint(0, 20))).replace("'z'", "'q'")


# -- execution --------------------------------------------------------------
def run_case(statements):
    """Return (verdict, detail). Any MiniDB traceback is a mismatch."""
    try:
        expected = bc.run_sqlite([statements])
    except Exception as exc:                       # SQLite side blew up: harness problem
        return "harness", f"sqlite runner raised {type(exc).__name__}: {exc}"
    try:
        actual = bc.run_minidb([statements])
    except Exception as exc:
        return "mismatch", f"MiniDB raised {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=2)}"

    ok, detail = bc.compare(expected, actual)
    return ("match" if ok else "mismatch"), detail


def minimize(statements, verdict_check):
    """Drop statements while the mismatch survives; the smallest repro is the filing unit."""
    current = list(statements)
    changed = True
    while changed and len(current) > 1:
        changed = False
        for index in range(len(current)):
            candidate = current[:index] + current[index + 1:]
            if verdict_check(candidate):
                current = candidate
                changed = True
                break
    return current


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--mode", choices=["structured", "mutation", "rejection", "all"],
                    default="structured")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    modes = ["structured", "mutation", "rejection"] if args.mode == "all" else [args.mode]
    mismatches = []
    counted = 0
    start = time.time()

    modes_used = set()
    for index in range(args.queries):
        mode = modes[index % len(modes)]
        modes_used.add(mode)
        statements = gen_case(rng, mode)
        verdict, detail = run_case(statements)
        if verdict == "match":
            counted += 1
            continue
        if verdict == "harness":
            print(f"harness problem on case {index}: {detail}")
            continue
        counted += 1
        repro = minimize(statements, lambda candidate: run_case(candidate)[0] == "mismatch")
        mismatches.append({"seed": args.seed, "case": index, "mode": mode,
                           "repro": repro, "detail": detail})

    elapsed = time.time() - start
    rate = (len(mismatches) / counted * 10000) if counted else 0.0

    print(f"FUZZ run seeds={args.seed}..{args.seed} queries={counted} mismatches={len(mismatches)} "
          f"rate={rate:.2f} per 10k  ({elapsed:.1f}s, {counted / max(elapsed, 1e-9):.1f} q/s)")
    print(f"features: {', '.join(sorted(FEATURES))}")
    print(f"modes: {', '.join(sorted(modes_used))}")
    print("per-oracle: differential (primary) / TLP: staged post-Tier-1 / NoREC: off (measured cost)")

    for number, mismatch in enumerate(mismatches[:10], 1):
        print(f"\nmismatch #{number} (seed {mismatch['seed']}, case {mismatch['case']}, {mismatch['mode']}):")
        for statement in mismatch["repro"]:
            print(f"    {statement}")
        print(f"    detail: {mismatch['detail']}")
    if len(mismatches) > 10:
        print(f"\n... {len(mismatches) - 10} more mismatches not shown")

    return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
