"""Pair two `tools/benchmark.py` outputs into a before/after table — and refuse to pair a lie.

A benchmark process can exit 0 and still leave an artifact that *exists and contains nothing* (a
killed run leaves the header it printed at startup; a SIGTERM'd one leaves a plausible prefix).
Quoting either would look like "the benchmark ran". So this tool asserts the shape before it
computes anything: every expected workload present, and every workload carrying a terminal line.

Three failure classes, kept apart on purpose:
  * a *missing* or *truncated* workload is a killed run                    -> exit 2, no table
  * a workload that DISCARDED (answers differed) or ERRORed is a finding
    to triage, and it is *present* — counting only OK-lines would read it
    as a killed run                                                     -> exit 3, named
  * an artifact whose own arithmetic disagrees with itself (ratio vs its
    medians) has been edited, truncated or interleaved                      -> exit 4

Usage:
    python3 tools/bench_pair.py before.txt after.txt
    python3 tools/bench_pair.py before.txt after.txt --workloads W1,W3,W6
"""

import argparse
import re
import statistics
import sys

WORKLOADS = ("W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W10")
EQUAL_BAND = 0.05          # within +-5% is equal: the box moves more than that between runs

HEADER = re.compile(r"^(W\d+)\s{2,}(.+)$")
ROWS = re.compile(r"\s{2,}rows=(\d+)")
LOADAVG = re.compile(r"\s{2,}loadavg=([\d.]+)")   # optional: older legs never wrote it
DISCARD = re.compile(r"DISCARDED:\s*(.*)$")
ERRORED = re.compile(r"ERROR:\s*(.*)$")
SQLITE = re.compile(r"^\s+sqlite_ms median=([\d.]+)")
MINIDB = re.compile(r"^\s+minidb_ms median=([\d.]+)")
RATIO = re.compile(r"^\s+ratio=([\d.]+)\s+output:\s*(\S+)")
RATIO_MIN = re.compile(r"\bratio_min=([\d.]+)")
BAND = re.compile(r"\bband=([\d.]+)")
RAW = re.compile(r"raw\d*=\[\s*([\d.\s]*)\]")   # `raw=` since the label went N-neutral; `raw7=` kept for old artifacts
HEADER_N = re.compile(r"\bN=(\d+)\b")
DEMO = re.compile(r"\bdemo=true\b")
SQLITE_LOADS = re.compile(r"^\s+sqlite_loads=\[\s*([\d.\s]*)\]")
MINIDB_LOADS = re.compile(r"^\s+minidb_loads=\[\s*([\d.\s]*)\]")
LOAD_SPAN = re.compile(r"\bload_span=([\d.]+)")
GATE_N = 11                # the freeze's floor: an N=7 artifact cannot certify a gate-2 call


def parse(path, require_header=True, allow_demo=False):
    """Return {workload: {...}} or raise ValueError naming the first malformed line.

    A file without the harness's own header is not a run. That check exists because a hand-made
    fixture paired silently once and its numbers were read as a finding — a fixture in a report is
    indistinguishable from a result unless the tool refuses to treat it as one.

    A `demo=true` header is that same fixture, self-declared. It is refused unless the caller says
    so explicitly (the `--allow-demo` escape, mirroring `--allow-foreign`), and gate certification
    never allows it at all: a fixture may be shown, never certified.
    """
    runs, current = {}, None
    seen_header = False
    header_n = None
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not seen_header:
                if not line.strip():
                    continue
                seen_header = True
                if require_header and not line.startswith("BENCH first-full-run"):
                    raise ValueError(f"{path}: not a benchmark artifact — first line is not the "
                                     f"harness header (pair a fixture with --allow-foreign)")
                declared = HEADER_N.search(line)
                if declared:
                    header_n = int(declared.group(1))
                if DEMO.search(line) and not allow_demo:
                    raise ValueError(f"{path}: this is a demo fixture (demo=true), not a run — "
                                     f"pass --allow-demo to show it as a fixture, never as a result")
            header = HEADER.match(line) if not line[:1].isspace() else None
            if header:
                # A workload section is present if *any* terminal line names it. A discarded or
                # errored workload is a finding to triage, not a hole in the artifact: counting
                # only OK-lines would read a legitimate discard as a killed run.
                name = header.group(1)
                if name in runs:
                    raise ValueError(f"{path}:{lineno}: workload {name} appears twice")
                title = header.group(2).strip()
                current = runs[name] = {"title": title, "status": None}
                if header_n is not None:
                    current["header_n"] = header_n
                rows = ROWS.search(title)
                if rows:
                    current["rows"] = int(rows.group(1))
                    current["title"] = title[:rows.start()].strip()
                    load = LOADAVG.search(title)
                    if load:
                        current["loadavg"] = float(load.group(1))
                    continue
                discard, errored = DISCARD.search(title), ERRORED.search(title)
                if discard:
                    current["status"], current["detail"] = "DISCARDED", discard.group(1)
                elif errored:
                    current["status"], current["detail"] = "ERROR", errored.group(1)
                else:
                    current["title"] = title
                continue
            for pattern, key in ((SQLITE, "sqlite_ms"), (MINIDB, "minidb_ms"), (RATIO, "ratio")):
                match = pattern.match(line)
                if match:
                    if current is None:
                        raise ValueError(f"{path}:{lineno}: {key} before any workload header")
                    if key == "ratio":
                        current["ratio"], current["status"] = float(match.group(1)), match.group(2)
                        gate = RATIO_MIN.search(line)
                        if gate:
                            current["ratio_min"] = float(gate.group(1))
                    else:
                        current[key] = float(match.group(1))
                        leg = "sq" if key == "sqlite_ms" else "md"
                        raw = RAW.search(line)
                        if raw:
                            current[f"raw_{leg}"] = [float(x) for x in raw.group(1).split()]
                        band = BAND.search(line)
                        if band:
                            current[f"band_{leg}"] = float(band.group(1))
                    break
            else:
                matched_loads = False
                for pattern, key in ((SQLITE_LOADS, "loads_sq"), (MINIDB_LOADS, "loads_md")):
                    match = pattern.match(line)
                    if match:
                        if current is None:
                            raise ValueError(f"{path}:{lineno}: {key} before any workload header")
                        current[key] = [float(x) for x in match.group(1).split()]
                        span = LOAD_SPAN.search(line)
                        if span:
                            current[key.replace("loads", "span")] = float(span.group(1))
                        matched_loads = True
                        break
                if not matched_loads:
                    stripped = line.strip()
                    if stripped.startswith(("sqlite_loads=", "minidb_loads=")):
                        leg = "sqlite" if stripped.startswith("sqlite_loads=") else "minidb"
                        body = stripped.split("[", 1)[1].split("]", 1)[0] if "[" in stripped else ""
                        tokens = body.split()
                        bad = next((i for i, token in enumerate(tokens) if token == "n/a"), None)
                        if bad is not None:
                            raise ValueError(f"{path}:{lineno}: load unreadable at sample {bad} on "
                                             f"the {leg} trace — trace incomplete")
                        raise ValueError(f"{path}:{lineno}: unparseable {leg} load trace")
    return runs


def check(label, runs, expected, gate=False):
    """The hollow-artifact gate: completeness asserted, never inferred from exit status.

    In gate mode the artifact must also carry everything the frozen judge reads: `ratio_min` (the
    gate quantity), both bands, both per-sample load traces, N at or above the floor, and a pre-load
    inside [1.0, 3.0]. Pairing never requires those: historical artifacts predate the fields and the
    freeze explicitly keeps the trend line readable.
    """
    problems = []
    missing = [name for name in expected if name not in runs]
    if missing:
        problems.append(f"{label}: missing workload(s) {', '.join(missing)}")
    for name in expected:
        run = runs.get(name)
        if run is None:
            continue
        if run["status"] is None:
            problems.append(f"{label}: workload {name} has no terminal line — partial row")
        elif run["status"] == "OK":
            keys = ["sqlite_ms", "minidb_ms", "ratio"]
            if gate:
                keys += ["ratio_min", "band_sq", "band_md", "span_sq", "span_md"]
            gaps = [key for key in keys if key not in run]
            if gaps:
                problems.append(f"{label}: workload {name} has no {', '.join(gaps)} — partial row")
            if gate:
                problems.extend(gate_shape(label, name, run))
    return problems


def gate_shape(label, name, run):
    """The frozen preconditions that can be checked from the artifact alone.

    Presence is not enough for the gate fields -- `consistency` re-derives them -- but a missing
    field is the wider failure (any pre-freeze artifact certifies as complete without it), so the
    two checks are kept apart on purpose.
    """
    problems = []
    declared_n = run.get("header_n")
    for key, leg in (("raw_sq", "sqlite"), ("raw_md", "minidb")):
        raw = run.get(key)
        if raw is None:
            problems.append(f"{label}: workload {name} has no raw samples for {leg}")
        else:
            if len(raw) < GATE_N:
                problems.append(f"{label}: workload {name} has N={len(raw)} on {leg}, below the "
                                f"frozen floor of {GATE_N}")
            if declared_n is not None and len(raw) != declared_n:
                problems.append(f"{label}: workload {name} declares N={declared_n} in the header "
                                f"but prints {len(raw)} raw samples for {leg}")
    for key, leg, raw_key in (("loads_sq", "sqlite", "raw_sq"), ("loads_md", "minidb", "raw_md")):
        loads = run.get(key)
        if loads is None:
            problems.append(f"{label}: workload {name} has no per-sample load trace for {leg}")
        elif run.get(raw_key) and len(loads) != len(run[raw_key]):
            problems.append(f"{label}: workload {name} has {len(loads)} load readings for "
                            f"{len(run[raw_key])} {leg} samples")
    load = run.get("loadavg")
    if load is None:
        problems.append(f"{label}: workload {name} has no pre-load reading")
    elif not (1.0 <= load <= 3.0):
        problems.append(f"{label}: workload {name} ran at loadavg={load:.2f}, outside the "
                        f"frozen [1.0, 3.0] window")
    return problems


def band_percent(values):
    """The same span the instrument emits: (max - min) / median, in percentage points."""
    median = statistics.median(values)
    return 0.0 if median == 0 else (max(values) - min(values)) / median * 100.0


def consistency(label, runs, expected, gate=False):
    """ratio must equal minidb_ms / sqlite_ms: an artifact whose own arithmetic disagrees with
    itself has been edited, truncated or interleaved, and its numbers cannot be paired.

    Gate mode additionally re-derives `ratio_min` and both bands from the published raw samples.
    A presence check would pass a false `ratio_min`; only recomputation kills it, which matters
    because `ratio_min` is the quantity the gate call reads.
    """
    problems = []
    for name in expected:
        run = runs.get(name)
        if run is None or "ratio" not in run:
            continue
        sq, md = run.get("raw_sq"), run.get("raw_md")
        derived = (statistics.median(md) / statistics.median(sq) if sq and md
                   else run["minidb_ms"] / run["sqlite_ms"] if run["sqlite_ms"] else float("inf"))
        if abs(derived - run["ratio"]) > max(0.01 * run["ratio"], 0.01):
            problems.append(f"{label}: workload {name} says ratio={run['ratio']:.3f} but its "
                            f"medians give {derived:.3f}")
        if not gate:
            continue
        if sq and md and "ratio_min" in run:
            derived_min = statistics.median(md) / min(sq)
            if abs(derived_min - run["ratio_min"]) > max(0.01 * run["ratio_min"], 0.01):
                problems.append(f"{label}: workload {name} says ratio_min={run['ratio_min']:.3f} "
                                f"but its raw samples give {derived_min:.3f}")
            if run["ratio_min"] < run["ratio"] - max(0.01 * run["ratio"], 0.01):
                problems.append(f"{label}: workload {name} says ratio_min={run['ratio_min']:.3f} "
                                f"< ratio={run['ratio']:.3f} — impossible, min <= median")
        for leg in ("sq", "md"):
            if f"raw_{leg}" in run and f"band_{leg}" in run:
                derived_band = band_percent(run[f"raw_{leg}"])
                if abs(derived_band - run[f"band_{leg}"]) > max(0.01 * derived_band, 0.01):
                    problems.append(f"{label}: workload {name} says band_{leg}="
                                    f"{run[f'band_{leg}']:.1f}% but its raw samples give "
                                    f"{derived_band:.1f}%")
            loads = run.get(f"loads_{leg}")
            if loads and f"span_{leg}" in run:
                derived_span = max(loads) - min(loads)
                # 0.011: both the trace and the field print at 2 dp, so a faithful span agrees
                # exactly; anything past one rounding step is a different trace or a stale field.
                if abs(derived_span - run[f"span_{leg}"]) > 0.011:
                    problems.append(f"{label}: workload {name} says span_{leg}="
                                    f"{run[f'span_{leg}']:.2f} but its load trace gives "
                                    f"{derived_span:.2f} — the trace moved further than the field "
                                    f"admits")
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
    parser.add_argument("after", nargs="?")
    parser.add_argument("--workloads", default=",".join(WORKLOADS),
                        help="comma-separated subset (default: all nine)")
    parser.add_argument("--gate-only", action="store_true",
                        help="assert one artifact's completeness, gate fields (ratio_min, bands, "
                             "per-sample loads, N floor, pre-load window) and self-consistency; "
                             "does not apply the threshold; no pairing")
    parser.add_argument("--allow-foreign", action="store_true",
                        help="pair a hand-made fixture that lacks the harness header (tests only; "
                             "never quote such a pairing as a result)")
    parser.add_argument("--allow-demo", action="store_true",
                        help="pair a fixture whose header says demo=true (shown, never quoted as a "
                             "result); gate certification ignores this flag")
    args = parser.parse_args(argv)
    expected = [name.strip() for name in args.workloads.split(",") if name.strip()]

    if args.gate_only:
        if args.allow_demo:
            print("bench_pair: --gate-only never certifies a demo fixture; drop --allow-demo",
                  file=sys.stderr)
            return 2
        try:
            runs = parse(args.before, not args.allow_foreign, allow_demo=False)
        except (OSError, ValueError) as exc:
            print(f"bench_pair: unreadable input: {exc}", file=sys.stderr)
            return 2
        problems = (check(args.before, runs, expected, gate=True)
                    + consistency(args.before, runs, expected, gate=True))
        if problems:
            print("bench_pair: artifact FAILS the gate:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 2
        print(f"artifact COMPLETE: {len(expected)}/{len(expected)} sections terminal, "
              f"self-consistent, answers OK")
        return 0

    try:
        require = not args.allow_foreign
        before, after = (parse(args.before, require, allow_demo=args.allow_demo),
                         parse(args.after, require, allow_demo=args.allow_demo))
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

    unpairable = [(name, side, run) for side, runs in (("before", before), ("after", after))
                  for name in expected
                  if (run := runs[name])["status"] != "OK"]
    if unpairable:
        print("bench_pair: refusing to pair — a workload is present but not a number:", file=sys.stderr)
        for name, side, run in unpairable:
            print(f"  - {name} ({side}): {run['status']}"
                  + (f" — {run.get('detail', '')[:80]}" if run.get("detail") else ""), file=sys.stderr)
        print("  (discarded/errored is a finding to triage; a missing section is a killed run)",
              file=sys.stderr)
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
