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

# A demo is a fixture, and a fixture shown anywhere must say so in the line that shows it: the very
# numbers below were once pasted into a report unlabelled and read for two rounds as a leg result.
DEMO_BANNER = ("DEMO -- synthetic numbers from a built-in fixture. No benchmark ran, no leg data is "
               "shown, and nothing here may be quoted as a result.")
# The reference medians and ratios below are the finished before leg's own, so the demo's table is
# a faithful replica of a real artifact: if the tool's arithmetic drifts, the demo stops matching
# the numbers the record already published.
DEMO_SQLITE = {"W1": 3.47, "W2": 39.27, "W3": 9.21, "W4": 14.56, "W5": 6.31,
               "W6": 69.05, "W7": 21.00, "W8": 10.07, "W10": 7.84}
DEMO_BASE = {"W1": 224694.208, "W2": 40.731, "W3": 90.423, "W4": 171.255, "W5": 199.669,
             "W6": 13.796, "W7": 305.249, "W8": 69769.476, "W10": 0.776}
# W6 and W7 improve as expected, W3 does not (an unmet expectation), W5 regresses on a stable
# reference -- one demo shows both failure species, and the tool calls the second one an engine
# finding before anything else.
DEMO_AFTER = dict(DEMO_BASE, W6=0.27, W7=13.0, W5=215.0)
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


def write_demo_leg(path, ratios):
    """A leg in the harness's printed shape, marked `demo=true` in its own header."""
    lines = ["BENCH first-full-run  module-sqlite 3.53.1  N=7  scale=100000  lookups=1000  demo=true", ""]
    for name in ORDER:
        ops = OPS.get(name, 1)
        sqlite_ms = DEMO_SQLITE[name]
        minidb_ms = sqlite_ms * ratios[name]
        lines += [f"{name}  synthetic {name:26s} rows={ops}",
                  f"     sqlite_ms median={sqlite_ms:.2f}",
                  f"     minidb_ms median={minidb_ms:.2f}",
                  f"     ratio={ratios[name]:.3f}  output: OK"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def demo():
    """Print the report against a built-in fixture, under a banner that says so first."""
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        before = write_demo_leg(os.path.join(scratch, "demo-before.txt"), DEMO_BASE)
        after = write_demo_leg(os.path.join(scratch, "demo-after.txt"), DEMO_AFTER)
        print(DEMO_BANNER)
        print()
        # allow_demo is set here and only here: the fixture is the tool's own, and the banner above
        # already says what it is. Any other reader of a demo file gets the refusal.
        main([before, after], allow_demo=True)
    print("\ndemo: a real report with these numbers would exit 1; this run exits 0 because nothing "
          "was measured.")
    return 0


def main(argv=None, allow_demo=False):
    parser = argparse.ArgumentParser(description="gate, compare, tabulate, and price a two-leg pair")
    parser.add_argument("before", nargs="?")
    parser.add_argument("after", nargs="?")
    parser.add_argument("--demo", action="store_true",
                        help="print a labelled fixture report (synthetic numbers, nothing measured)")
    parser.add_argument("--expect", default="", help="NAME=equal|improve|regress,... (overrides defaults)")
    args = parser.parse_args(argv)
    if args.demo:
        return demo()
    if not args.before or not args.after:
        parser.error("two leg artifacts are required (or --demo for a labelled fixture)")
    expect = parse_expect(args.expect)

    parsed = {}
    for label, path in (("before", args.before), ("after", args.after)):
        try:
            runs = bench_pair.parse(path, allow_demo=allow_demo)
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
        # A regression and a miss are different species: only a regression touches the parity
        # clause, and it can only be judged with its own reference-side delta beside it. A ratio
        # that rose while the reference rose is a cross-box reading; one that rose against a stable
        # reference is an engine finding and reaches the gate before anything else does.
        regressions = [item for item in failures if item[2] == "regress"]
        misses = [item for item in failures if item[2] != "regress"]
        if regressions:
            print("\nregressions -- the parity clause's event, named separately:")
            for name, _want, _got in regressions:
                b, a = before[name], after[name]
                moved = ((a["sqlite_ms"] - b["sqlite_ms"]) / b["sqlite_ms"]) if b["sqlite_ms"] else 0.0
                side = ("cross-box reading: the reference moved with it" if abs(moved) > BAND
                        else "stable reference: engine side -- a gate finding before anything else")
                print(f"  {name}  ratio {b['ratio']:.3f} -> {a['ratio']:.3f} "
                      f"({(a['ratio'] / b['ratio'] - 1.0):+.1%})  reference {moved:+.1%}  -- {side}")
        if misses:
            print("\nunmet expectations that are not regressions:")
            for name, want, got in misses:
                print(f"  {name}  wanted {want}, got {got}")
        return 1
    print("\nverdict: every workload landed as frozen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
