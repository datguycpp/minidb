"""Pair two `tools/benchmark.py` outputs into a before/after table — and refuse to pair a lie.

A benchmark process can exit 0 and still leave an artifact that *exists and contains nothing* (a
killed run leaves the header it printed at startup; a SIGTERM'd one leaves a plausible prefix).
Quoting either would look like "the benchmark ran". So this tool asserts the shape before it
computes anything: every expected workload present, every workload carrying its medians, its
ratio, and `output: OK`. A missing or partial workload is a hard failure (exit 2), never a shorter
table; a workload whose answers differed is a correctness failure (exit 3), never a ratio.

Usage:
    python3 tools/bench_pair.py before.txt after.txt
    python3 tools/bench_pair.py before.txt after.txt --workloads W1,W3,W6
"""

import argparse
import re
import sys

WORKLOADS = ("W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W10")
EQUAL_BAND = 0.05          # within +-5% is equal: the box moves more than that between runs

HEADER = re.compile(r"^(W\d+)\s{2,}(.+?)\s{2,}rows=(\d+)\s*$")
SQLITE = re.compile(r"^\s+sqlite_ms median=([\d.]+)")
MINIDB = re.compile(r"^\s+minidb_ms median=([\d.]+)")
RATIO = re.compile(r"^\s+ratio=([\d.]+)\s+output:\s*(\S+)")


def parse(path):
    """Return {workload: {...}} or raise ValueError naming the first malformed line."""
    runs, current = {}, None
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            header = HEADER.match(line)
            if header:
                name = header.group(1)
                if name in runs:
                    raise ValueError(f"{path}:{lineno}: workload {name} appears twice")
                current = runs[name] = {"title": header.group(2).strip(),
                                        "rows": int(header.group(3))}
                continue
            for pattern, key in ((SQLITE, "sqlite_ms"), (MINIDB, "minidb_ms"), (RATIO, "ratio")):
                match = pattern.match(line)
                if match:
                    if current is None:
                        raise ValueError(f"{path}:{lineno}: {key} before any workload header")
                    if key == "ratio":
                        current["ratio"], current["output"] = float(match.group(1)), match.group(2)
                    else:
                        current[key] = float(match.group(1))
                    break
    return runs


def check(label, runs, expected):
    """The hollow-artifact gate: completeness asserted, never inferred from exit status."""
    problems = []
    missing = [name for name in expected if name not in runs]
    if missing:
        problems.append(f"{label}: missing workload(s) {', '.join(missing)}")
    for name in expected:
        run = runs.get(name)
        if run is None:
            continue
        gaps = [key for key in ("sqlite_ms", "minidb_ms", "ratio", "output") if key not in run]
        if gaps:
            problems.append(f"{label}: workload {name} has no {', '.join(gaps)} — partial row")
    return problems


def consistency(label, runs, expected):
    """ratio must equal minidb_ms / sqlite_ms: an artifact whose own arithmetic disagrees with
    itself has been edited, truncated or interleaved, and its numbers cannot be paired."""
    problems = []
    for name in expected:
        run = runs.get(name)
        if run is None or "ratio" not in run:
            continue
        derived = run["minidb_ms"] / run["sqlite_ms"] if run["sqlite_ms"] else float("inf")
        if abs(derived - run["ratio"]) > max(0.01 * run["ratio"], 0.01):
            problems.append(f"{label}: workload {name} says ratio={run['ratio']:.3f} but its "
                            f"medians give {derived:.3f}")
    return problems


def verdict(before, after):
    fold = before / after if after else float("inf")
    if fold > 1 + EQUAL_BAND:
        return fold, "improved"
    if fold < 1 - EQUAL_BAND:
        return fold, "REGRESSED"
    return fold, "equal"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--workloads", default=",".join(WORKLOADS),
                        help="comma-separated subset (default: all nine)")
    args = parser.parse_args(argv)
    expected = [name.strip() for name in args.workloads.split(",") if name.strip()]

    try:
        before, after = parse(args.before), parse(args.after)
    except (OSError, ValueError) as exc:
        print(f"bench_pair: unreadable input: {exc}", file=sys.stderr)
        return 2

    problems = check(args.before, before, expected) + check(args.after, after, expected)
    if problems:
        print("bench_pair: refusing to pair — the artifact is not complete:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("  (a header-only or partial file is a failed run, regardless of its exit code)",
              file=sys.stderr)
        return 2

    broken = consistency(args.before, before, expected) + consistency(args.after, after, expected)
    if broken:
        print("bench_pair: refusing to pair — the artifact contradicts itself:", file=sys.stderr)
        for problem in broken:
            print(f"  - {problem}", file=sys.stderr)
        return 4

    bad = [name for name in expected if before[name]["output"] != "OK" or after[name]["output"] != "OK"]
    if bad:
        print(f"bench_pair: refusing to pair — answers differed on {', '.join(bad)}", file=sys.stderr)
        return 3

    print(f"{'W':4s} {'workload':28s} {'sqlite ms':>17s} {'minidb ms':>19s} "
          f"{'ratio':>15s} {'fold':>7s}  verdict")
    print(f"{'':4s} {'':28s} {'before -> after':>17s} {'before -> after':>19s} "
          f"{'before -> after':>15s}")
    for name in expected:
        b, a = before[name], after[name]
        fold, word = verdict(b["ratio"], a["ratio"])
        ref = (a["sqlite_ms"] / b["sqlite_ms"] - 1) * 100 if b["sqlite_ms"] else 0.0
        print(f"{name:4s} {b['title'][:28]:28s} "
              f"{b['sqlite_ms']:7.2f} -> {a['sqlite_ms']:<7.2f} "
              f"{b['minidb_ms']:8.2f} -> {a['minidb_ms']:<8.2f} "
              f"{b['ratio']:7.3f} -> {a['ratio']:<7.3f} {fold:6.2f}x  {word}"
              f"   [reference {ref:+.0f}%]")
    print("\nfold > 1 means MiniDB improved relative to SQLite; the bracketed reference delta is the "
          "unchanged engine's own movement — the noise probe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
