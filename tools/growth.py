#!/usr/bin/env python3
"""Growth run: three-table chains and aggregates in correlated subqueries.

The ladder's corpus grew one surface at a time; this tool grows the two shapes the ladder never
asked in combination -- join chains of depth three, and aggregates inside correlated subqueries --
and asks the reference the same questions. It is a generator plus a differential runner, not a
conformance suite: every disagreement is a candidate finding, and a disagreement that survives
minimization is a repro for the corpus.

Definition of a mismatch is the fuzz spec's, unchanged: MiniDB's answer differs from the pinned
SQLite build's answer under the harness's own comparator, or MiniDB raises where SQLite does not.

Run:
    python3 tools/growth.py --cases 200 --seed 1 --label smoke
    python3 tools/growth.py --cases 1000 --seeds 1,2,3 --out growth-findings.json
"""

import argparse
import json
import os
import random
import string
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402

KINDS = ("chain2", "chain2_group", "composite_on", "corr_in_select", "corr_in_where",
         "corr_nested", "corr_grouped_outer", "corr_two_aggregates")


# -- generation -------------------------------------------------------------
def gen_schema(rng):
    """Three tables sharing (id, k1, k2) so chains and composite keys can be built from them."""
    statements = []
    for name in ("a", "b", "c"):
        statements.append(f"CREATE TABLE {name} (id INTEGER, k1 INTEGER, k2 INTEGER, v INTEGER, s TEXT)")
        rows = []
        count = rng.randint(1, 10)
        for row_id in range(1, count + 1):
            k1 = "NULL" if rng.random() < 0.15 else str(rng.randint(1, 3))
            k2 = "NULL" if rng.random() < 0.15 else str(rng.randint(1, 3))
            v = "NULL" if rng.random() < 0.2 else str(rng.randint(0, 9))
            s = "NULL" if rng.random() < 0.15 else f"'{rng.choice(string.ascii_lowercase[:4])}'"
            # ids repeat across tables on purpose: fan-out is where chains and correlated
            # aggregates disagree most often, and duplicates are legal in this schema.
            rows.append(f"({row_id}, {k1}, {k2}, {v}, {s})")
        statements.append(f"INSERT INTO {name} VALUES {', '.join(rows)}")
    return statements


def gen_query(rng, kind):
    agg = rng.choice(["SUM(b.v)", "COUNT(*)", "COUNT(b.v)", "MAX(b.v)", "MIN(b.v)", "AVG(b.v)"])
    cmp_op = rng.choice([">", ">=", "<", "<=", "="])
    threshold = rng.randint(0, 6)

    if kind == "chain2":
        where = rng.choice(["", " WHERE a.v IS NOT NULL", " WHERE b.v > 3", " WHERE c.s = 'a'"])
        return (f"SELECT a.id, b.v, c.s FROM a JOIN b ON a.id = b.id JOIN c ON b.id = c.id"
                f"{where} ORDER BY a.id, b.v, c.s")

    if kind == "chain2_group":
        having = rng.choice(["", " HAVING COUNT(*) > 1", " HAVING SUM(b.v) > 5"])
        return (f"SELECT a.k1, COUNT(*), SUM(b.v) FROM a JOIN b ON a.k1 = b.k1 "
                f"JOIN c ON b.k1 = c.k1 GROUP BY a.k1{having} ORDER BY 1")

    if kind == "composite_on":
        return (f"SELECT a.id, b.v FROM a JOIN b ON a.k1 = b.k1 AND a.k2 = b.k2 "
                f"ORDER BY a.id, b.v")

    if kind == "corr_in_select":
        return (f"SELECT a.id, (SELECT {agg} FROM b WHERE b.k1 = a.k1) AS x "
                f"FROM a ORDER BY a.id")

    if kind == "corr_in_where":
        return (f"SELECT a.id FROM a WHERE (SELECT {agg} FROM b WHERE b.k2 = a.k2) "
                f"{cmp_op} {threshold} ORDER BY a.id")

    if kind == "corr_nested":
        # depth two: the correlated aggregate's own predicate contains another subquery
        inner = rng.choice(["SELECT b.v FROM c WHERE c.k1 = a.k1",
                            "SELECT MAX(c.v) FROM c WHERE c.k2 = a.k2"])
        return (f"SELECT a.id, (SELECT COUNT(*) FROM b WHERE b.v IN ({inner})) AS x "
                f"FROM a ORDER BY a.id")

    if kind == "corr_grouped_outer":
        return (f"SELECT a.k1, COUNT(*), (SELECT {agg} FROM b WHERE b.k1 = a.k1) AS x "
                f"FROM a GROUP BY a.k1 ORDER BY 1")

    # corr_two_aggregates: two correlated subqueries in one projection. Each aggregate reads its
    # own subquery's table -- the first version reused the b-side expression inside the c-side
    # subquery, which is invalid SQL and became a finding of its own (lazy column validation
    # accepts it on an empty relation); a generator bug must not masquerade as an engine gap.
    second = rng.choice(["COUNT(*)", "SUM(c.v)", "MAX(c.v)"])
    return (f"SELECT a.id, (SELECT {agg} FROM b WHERE b.k1 = a.k1) AS x, "
            f"(SELECT {second} FROM c WHERE c.k2 = a.k2) AS y FROM a ORDER BY a.id")


def gen_case(rng):
    statements = gen_schema(rng)
    kind = rng.choice(KINDS)
    statements.append(gen_query(rng, kind))
    return kind, statements


# -- execution --------------------------------------------------------------
def run_case(statements):
    """(verdict, detail, sqlite_answer, minidb_answer); any MiniDB traceback is a mismatch."""
    try:
        expected = bc.run_sqlite([statements])
    except Exception as exc:
        return "harness", f"sqlite runner raised {type(exc).__name__}: {exc}", None, None
    try:
        actual = bc.run_minidb([statements])
    except Exception as exc:
        return ("mismatch", f"MiniDB raised {type(exc).__name__}: {exc}\n"
                            f"{traceback.format_exc(limit=2)}", expected, "raised")
    ok, detail = bc.compare(expected, actual)
    return ("match" if ok else "mismatch"), detail, expected, actual


def signature(detail):
    """The comparison's first line, minus the values that legitimately move under minimization."""
    first = str(detail).splitlines()[0]
    return first[:60]


def survives(statements, wanted):
    """A minimized repro must fail the same way; otherwise the minimizer invents a second bug."""
    verdict, detail, _, _ = run_case(statements)
    return verdict == "mismatch" and signature(detail) == wanted


def minimize(statements, detail):
    """Drop statements while the same mismatch survives; the smallest repro is the filing unit."""
    wanted = signature(detail)
    current = list(statements)
    changed = True
    while changed and len(current) > 2:
        changed = False
        for index in range(len(current)):
            candidate = current[:index] + current[index + 1:]
            if not candidate:
                continue
            if survives(candidate, wanted):
                current, changed = candidate, True
                break
    return current


def main(argv=None):
    parser = argparse.ArgumentParser(description="grow the two unasked shapes and diff against SQLite")
    parser.add_argument("--cases", type=int, default=200)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--seeds", default="", help="comma-separated seeds (overrides --seed)")
    parser.add_argument("--label", default="", help="what this run is, printed in the summary")
    parser.add_argument("--out", default="", help="write findings as JSON here")
    parser.add_argument("--no-minimize", action="store_true")
    args = parser.parse_args(argv)

    seeds = [int(s) for s in args.seeds.split(",") if s] or [args.seed]
    findings, total, matches = [], 0, 0
    per_kind = {}
    for seed in seeds:
        rng = random.Random(seed)
        for index in range(args.cases):
            kind, statements = gen_case(rng)
            total += 1
            verdict, detail, expected, actual = run_case(statements)
            per_kind.setdefault(kind, [0, 0])
            if verdict == "match":
                per_kind[kind][0] += 1
                matches += 1
                continue
            per_kind[kind][1] += 1
            repro = statements if args.no_minimize else minimize(statements, detail)
            findings.append({"seed": seed, "case": index, "kind": kind, "verdict": verdict,
                             "detail": detail, "statements": repro,
                             "minimized_from": len(statements), "minimized_to": len(repro),
                             "sqlite": expected, "minidb": actual})
            print(f"[{verdict}] seed={seed} case={index} kind={kind} "
                  f"({len(statements)} -> {len(repro)} statements)")
            for line in repro:
                print(f"    {line}")
            print(f"    detail: {str(detail).splitlines()[0][:300]}")

    print(f"\ngrowth run{' (' + args.label + ')' if args.label else ''}: {total} cases, "
          f"{matches} matched, {len(findings)} mismatched, seeds {seeds}")
    for kind in KINDS:
        if kind in per_kind:
            ok, bad = per_kind[kind]
            print(f"  {kind:20s} {ok:4d} match  {bad:4d} mismatch")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"cases": total, "matches": matches, "seeds": seeds,
                       "findings": findings}, handle, indent=1)
        print(f"findings written to {args.out}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
