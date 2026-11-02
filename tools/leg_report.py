"""Report a two-leg benchmark pair in the reading order the record uses.

    gate -> cross-leg comparability -> table -> absolutes

Each stage exists because the one before it cannot answer its question. The gate refuses an
incomplete or self-contradictory file, so no ratio is computed from a snapshot. The comparability
pass compares the two legs' *reference* medians per workload, because an in-leg ratio normalizes
box speed only to the extent both engines scale with it; a reference that moved beyond the band
means the workload's ratio is a comparison across two different boxes and must be read with that
caveat attached, never quietly. The table then classifies each workload against the frozen
expectation, and the absolutes come last because a ratio answers "relative to the reference" while
a mechanism claim ("the scan is gone") needs milliseconds.

Usage:
    python3 tools/leg_report.py before.txt after.txt
    python3 tools/leg_report.py before.txt after.txt --expect W7=improve,W3=improve,W6=improve
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_pair  # noqa: E402  (sibling tool, not a package import)

ORDER = ("W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W10")
# Operations each workload performs per run, so a median can be read per operation as well as
# per run: W1 does 1,000 lookups, W2 two scans, W7 ten thousand single-row commits.
OPS = {"W1": 1000, "W2": 2, "W3": 1, "W4": 1, "W5": 1, "W6": 1, "W7": 10000, "W8": 1000, "W10": 1000}
ROWS = 100000
BAND = bench_pair.EQUAL_BAND

# The frozen expectation for the shipped tree against the M38 entry commit. Anything not named
# here is expected equal: the rewrite's scope is the aggregate loop, and the write-path item's is
# the uniqueness set, so every other path should not move.
DEFAULT_EXPECT = {"W3": "improve", "W6": "improve", "W7": "improve"}
ATTRIBUTION = {
    "W3": "C module (M38)",
    "W6": "C module (M38)",
    "W7": "write-path uniqueness set (post-M38)",
}


def gate(path, runs):
    """Return a list of problems; empty means the file may be read as an artifact."""
    expected = list(bench_pair.WORKLOADS)
    return bench_pair.check(path, runs, expected) + bench_pair.consistency(path, runs, expected)


def classify(before_ms, after_ms):
    """Compare two ratios: improved, regressed, or equal inside the declared band."""
    if after_ms <= before_ms * (1.0 - BAND):
        return "improve"
    if after_ms >= before_ms * (1.0 + BAND):
        return "regress"
    return "equal"


def comparability(before, after):
    """Workloads whose reference median moved beyond the band between legs."""
    moved = []
    for name in ORDER:
        if name not in before or name not in after:
            continue
        ref_before, ref_after = before[name]["sqlite_ms"], after[name]["sqlite_ms"]
        if ref_before <= 0:
            continue
        delta = (ref_after - ref_before) / ref_before
        if abs(delta) > BAND:
            moved.append((name, delta))
    return moved


def parse_expect(text):
    expect = dict(DEFAULT_EXPECT)
    if not text:
        return expect
    for item in text.split(","):
        name, _, verdict = item.partition("=")
        if not name or verdict not in ("equal", "improve", "regress"):
            raise SystemExit(f"leg_report: bad --expect item {item!r} (NAME=equal|improve|regress)")
        expect[name.strip()] = verdict
    return expect


def main(argv=None):
    parser = argparse.ArgumentParser(description="gate, compare, tabulate, and price a two-leg pair")
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--expect", default="", help="NAME=equal|improve|regress,... (overrides defaults)")
    args = parser.parse_args(argv)
    expect = parse_expect(args.expect)

    parsed = {}
    for label, path in (("before", args.before), ("after", args.after)):
        try:
            runs = bench_pair.parse(path)
        except (OSError, ValueError) as exc:
            print(f"leg_report: {label} unreadable: {exc}", file=sys.stderr)
            return 2
        problems = gate(path, runs)
        if problems:
            print(f"leg_report: {label} FAILS the gate, no ratio will be computed:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 2
        parsed[label] = runs
    before, after = parsed["before"], parsed["after"]
    print(f"gate: both legs complete, {len(ORDER)}/{len(ORDER)} sections terminal, self-consistent, answers OK")

    moved = comparability(before, after)
    if moved:
        print("comparability: reference side moved beyond +/-5% on "
              + ", ".join(f"{name} ({delta:+.1%})" for name, delta in moved)
              + " -- read those ratios with the cross-box caveat attached")
    else:
        print("comparability: reference side within +/-5% on every workload")

    print(f"\n{'':4s} {'sqlite before':>13s} {'after':>9s} | {'minidb before':>14s} {'after':>12s} "
          f"| {'ratio before':>12s} {'after':>10s} | {'per-op after':>12s} | verdict")
    failures = []
    for name in ORDER:
        if name not in before or name not in after:
            continue
        b, a = before[name], after[name]
        verdict = classify(b["ratio"], a["ratio"])
        want = expect.get(name, "equal")
        per_op = a["minidb_ms"] / OPS.get(name, 1)
        note = ATTRIBUTION.get(name, "")
        ok = (verdict == want) if want != "equal" else (verdict == "equal")
        if not ok:
            failures.append((name, want, verdict))
        mark = "ok" if ok else f"MISS (expected {want})"
        print(f"{name:4s} {b['sqlite_ms']:13.2f} {a['sqlite_ms']:9.2f} | {b['minidb_ms']:14.2f} "
              f"{a['minidb_ms']:12.2f} | {b['ratio']:12.3f} {a['ratio']:10.3f} | {per_op:12.3f} ms "
              f"| {verdict} {mark}{(' -- ' + note) if note else ''}")

    print("\nabsolutes (milliseconds, independent of the reference):")
    for name, label in (("W1", "per lookup"), ("W8", "per update"), ("W7", "per commit")):
        if name in before and name in after:
            print(f"  {name} {label:11s} before {before[name]['minidb_ms'] / OPS[name]:10.3f} ms"
                  f"   after {after[name]['minidb_ms'] / OPS[name]:10.3f} ms")
    for name in ("W1", "W8"):
        if name in after:
            per_row = after[name]["minidb_ms"] / OPS[name] / ROWS * 1000.0
            print(f"  {name} per row-visit  {per_row:.3f} us  ({ROWS:,} rows)")

    if failures:
        print("\nverdict: the frozen expectation was not met for "
              + ", ".join(f"{name} (wanted {want}, got {got})" for name, want, got in failures))
        return 1
    print("\nverdict: every workload landed as frozen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
