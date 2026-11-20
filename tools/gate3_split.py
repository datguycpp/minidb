"""Gate 3's decomposition instrument: paired PARSEONLY / LOOP, with the residue as a CI.

The question this answers, and the only one it answers: **does the execution residue alone sit
under gate 3's ceiling?** If it does, a perfect parser could in principle pass the gate; if it does
not, parser work cannot pass on its own. That is a reachability question, so it is decided on an
interval, not on two medians: the residue is paired per round (loop_i - parse_i, same session, same
box state), and the reachability rule freezes as **the lower bound of the residual median's 95%
bootstrap CI clears the ceiling** — the point estimate clearing it is not enough.

Not a gate call. It issues no pass/fail for goal 6, and its numbers never enter a ceiling.

Constants are the BLOCK-engine precedent: seeded percentile bootstrap of the median, seed 20261104,
20,000 resamples, 2.5th/97.5th percentiles, so any seat reproduces the interval from the printed
per-round raws. Leg order alternates each round, per-sample loads are read fresh, and the drift rule
(window [1.0, 3.0], span <= 1.0) applies to the trace: a run that trips it is INCONCLUSIVE.

Exit codes: 0 reachable or excluded (a verdict was reached), 2 unusable input, 3 window/drift.
"""

import argparse
import hashlib
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minidb.engine as engine  # noqa: E402
from minidb.engine import Database  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_COUNT = 10000
TEXTS = [f"SELECT count(*) FROM t WHERE id > {i}" for i in range(TEXT_COUNT)]
TINY = os.path.join(REPO, "tests", "skeptic", "fixtures", "g6-tiny.db")
TINY_PIN = (12288, "64a9d5417b7ec44389ffbde5b7e6da52ec53aff157156fbfd3e3caed3a4d4c30")
CEILING = 1934.4
BOOTSTRAP_SEED = 20261104
BOOTSTRAP_RESAMPLES = 20000
WINDOW = (1.0, 3.0)
DRIFT_SPAN = 1.0


def loadavg():
    with open("/proc/loadavg", encoding="ascii") as handle:
        return float(handle.read().split()[0])


def identity(path):
    with open(path, "rb") as handle:
        data = handle.read()
    return len(data), hashlib.sha256(data).hexdigest()


def band(values):
    median = statistics.median(values)
    return 0.0 if median == 0 else (max(values) - min(values)) / median * 100.0


def bootstrap_ci(values, seed=BOOTSTRAP_SEED, resamples=BOOTSTRAP_RESAMPLES):
    rng = random.Random(seed)
    n = len(values)
    medians = []
    for _ in range(resamples):
        medians.append(statistics.median([values[rng.randrange(n)] for _ in range(n)]))
    medians.sort()
    return medians[int(0.025 * resamples)], medians[int(0.975 * resamples) - 1]


def parse_only():
    engine._parse_cached.cache_clear()
    for sql in TEXTS:
        engine._parse_cached(sql)


def loop(probe=None):
    """The 10k statements through `db.run`. With a probe dict, the access-decision region is timed
    by the engine itself (planning separated from execution), and the plan-cache path that served
    is counted."""
    db = Database(TINY)
    try:
        db.plan_probe = probe
        for sql in TEXTS:
            db.run(sql)
        return probe
    finally:
        db.close()


def classify(low, high, ceiling):
    """The frozen reachability rule: the interval decides, not the point estimate."""
    if low > ceiling:
        return "PARSER-ONLY CANNOT PASS — residue CI low %.1f > ceiling %.1f (rule: lower bound clears)" % (low, ceiling)
    if high < ceiling:
        return "PARSER WORK ALONE COULD PASS — residue CI high %.1f < ceiling %.1f" % (high, ceiling)
    return "NOT ESTABLISHED — residue CI [%.1f, %.1f] spans the ceiling %.1f" % (low, high, ceiling)


def timed(fn):
    started = time.perf_counter()
    fn()
    return (time.perf_counter() - started) * 1000.0


def _count_cache_leg(leg, before, after, cache_legs, cleared):
    """Count one leg's parse-cache activity, leg-aware.

    A parse leg clears the cache at its start, so its post-leg totals ARE the leg; a delta would
    read zero for the second of two back-to-back parse legs, which the alternating order produces
    every other round -- that is how five paired rounds vanished once. The loop leg is read as a
    delta, which is only valid while it never clears: a shrinking total is recorded in `cleared` so
    the caller can refuse a verdict instead of silently counting a leg as zero.
    """
    if leg == "parse":
        cache_legs[leg][0] += after.hits
        cache_legs[leg][1] += after.misses
        return
    if after.hits + after.misses < before.hits + before.misses:
        cleared.append(leg)
    cache_legs[leg][0] += after.hits - before.hits
    cache_legs[leg][1] += after.misses - before.misses


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0] or "")
    parser.add_argument("--n", type=int, default=11)
    parser.add_argument("--ceiling", type=float, default=CEILING)
    # parse_gap's discriminator: the loop allocates plans, rows and environments between parses
    # while the parse leg does not, so generational collections may be landing inside the parse
    # window. Alternate GC state per round in one session and compare the gap by state.
    parser.add_argument("--gc", choices=("on", "off", "alternate"), default="on")
    args = parser.parse_args(argv)

    if not os.path.exists(TINY):
        print("gate3_split: no fixture at %s" % TINY, file=sys.stderr)
        return 2
    digest = identity(TINY)
    if digest != TINY_PIN:
        print("gate3_split: fixture identity is %s, not the pinned %s" % (digest, TINY_PIN),
              file=sys.stderr)
        return 2
    if args.n < 11:
        print("gate3_split: N=%d is below the floor of 11 — measure only, never cite" % args.n,
              file=sys.stderr)

    (parse_ms, loop_ms, prep_ms, scan_ms, rest_ms, filter_ms, complex_ms,
     outside_ms, loop_parse_ms, exec_wrap_ms, exec_rest2_ms, loads) = (
        [], [], [], [], [], [], [], [], [], [], [], [])
    aggregate_path = 0
    hits_total = misses_total = 0
    gc_gap = {"on": [], "off": []}
    gc_parse_gap = {"on": [], "off": []}
    cache_legs = {"loop": [0, 0], "parse": [0, 0]}
    counter_cleared = []
    import gc as _gc
    for i in range(args.n):
        if args.gc == "alternate":
            _gc.collect()
            if i % 2 == 0:
                _gc.disable()
            else:
                _gc.enable()
        gc_state = "off" if not _gc.isenabled() else "on"
        loads.append(loadavg())
        probe = {"prep_ns": 0, "scan_ns": 0, "filter_ns": 0, "complex_ns": 0, "aggregate": 0,
                 "run_parse_ns": 0, "run_exec_ns": 0, "exec_body_ns": 0,
                 "simple_ns": 0, "simple_hits": 0, "simple_misses": 0}
        parse_first = i % 2 == 0

        def _count_cache(leg, before):
            _count_cache_leg(leg, before, engine._parse_cached.cache_info(), cache_legs,
                             counter_cleared)

        if parse_first:
            before = engine._parse_cached.cache_info()
            parse_ms.append(timed(parse_only))
            _count_cache("parse", before)
            before = engine._parse_cached.cache_info()
            loop_ms.append(timed(lambda: loop(probe)))
            _count_cache("loop", before)
        else:
            before = engine._parse_cached.cache_info()
            loop_ms.append(timed(lambda: loop(probe)))
            _count_cache("loop", before)
            before = engine._parse_cached.cache_info()
            parse_ms.append(timed(parse_only))
            _count_cache("parse", before)
        prep = probe["prep_ns"] / 1e6
        scan = probe["scan_ns"] / 1e6
        filt = probe["filter_ns"] / 1e6
        whole = probe["complex_ns"] / 1e6
        prep_ms.append(prep)
        scan_ms.append(scan)
        filter_ms.append(filt)
        complex_ms.append(whole)
        # Inside the aggregate path: what is left after prep, scan and the predicate application --
        # projection, aggregation, finalise and the path's own validation.
        rest_ms.append(whole - prep - scan - filt)
        # Outside it: everything else between the timed loop and the aggregate path -- parse-cache
        # lookup, statement dispatch, transaction and connection overhead.
        outside_ms.append(loop_ms[-1] - parse_ms[-1] - whole)
        # Inside run(): the parse lookup, execute's own wrapper, and the body outside the aggregate
        # path. These are measured where the statement actually passes, not inferred.
        loop_parse_ms.append(probe["run_parse_ns"] / 1e6)
        gc_gap[gc_state].append(loop_ms[-1] - parse_ms[-1])
        gc_parse_gap[gc_state].append(loop_parse_ms[-1] - parse_ms[-1])
        exec_wrap_ms.append((probe["run_exec_ns"] - probe["exec_body_ns"]) / 1e6)
        exec_rest2_ms.append((probe["exec_body_ns"] - probe["complex_ns"]) / 1e6)
        aggregate_path += probe["aggregate"]
        hits_total += probe["simple_hits"]
        misses_total += probe["simple_misses"]

    residue = [lo - pa for pa, lo in zip(parse_ms, loop_ms)]
    median_parse = statistics.median(parse_ms)
    median_loop = statistics.median(loop_ms)
    median_prep = statistics.median(prep_ms)
    median_scan = statistics.median(scan_ms)
    median_rest = statistics.median(rest_ms)
    median_filter = statistics.median(filter_ms)
    median_outside = statistics.median(outside_ms)
    median_loop_parse = statistics.median(loop_parse_ms)
    median_exec_wrap = statistics.median(exec_wrap_ms)
    median_exec_rest2 = statistics.median(exec_rest2_ms)
    gap_ms = [a - b for a, b in zip(loop_parse_ms, parse_ms)]
    median_residue = statistics.median(residue)
    low, high = bootstrap_ci(residue)
    prep_low, prep_high = bootstrap_ci(prep_ms)
    scan_low, scan_high = bootstrap_ci(scan_ms)
    rest_low, rest_high = bootstrap_ci(rest_ms)
    filter_low, filter_high = bootstrap_ci(filter_ms)
    outside_low, outside_high = bootstrap_ci(outside_ms)
    loop_parse_low, loop_parse_high = bootstrap_ci(loop_parse_ms)
    exec_wrap_low, exec_wrap_high = bootstrap_ci(exec_wrap_ms)
    exec_rest2_low, exec_rest2_high = bootstrap_ci(exec_rest2_ms)
    span = max(loads) - min(loads)

    print("gate3_split  statements=%d  N=%d  ceiling=%.1f  fixture_sha256=%s"
          % (TEXT_COUNT, args.n, args.ceiling, digest[1]))
    print("loads=[ %s ]  window=[%.1f, %.1f]  span=%.2f"
          % (" ".join("%.2f" % x for x in loads), WINDOW[0], WINDOW[1], span))
    print("     parse_ms  median=%.1f  raw=[ %s ]  band=%.1f%%  per_stmt=%.1f us"
          % (median_parse, " ".join("%.1f" % x for x in parse_ms), band(parse_ms),
             median_parse * 1000.0 / TEXT_COUNT))
    print("     loop_ms   median=%.1f  raw=[ %s ]  band=%.1f%%  per_stmt=%.1f us"
          % (median_loop, " ".join("%.1f" % x for x in loop_ms), band(loop_ms),
             median_loop * 1000.0 / TEXT_COUNT))
    print("     residue   median=%.1f  raw=[ %s ]  band=%.1f%%  per_stmt=%.1f us"
          % (median_residue, " ".join("%.1f" % x for x in residue), band(residue),
             median_residue * 1000.0 / TEXT_COUNT))
    print("     residue 95%% CI (bootstrap, seed=%d, %d resamples)=[%.1f, %.1f]"
          % (BOOTSTRAP_SEED, BOOTSTRAP_RESAMPLES, low, high))
    print("     prep      median=%.1f  raw=[ %s ]  CI=[%.1f, %.1f]  per_stmt=%.1f us"
          % (median_prep, " ".join("%.1f" % x for x in prep_ms), prep_low, prep_high,
             median_prep * 1000.0 / TEXT_COUNT))
    print("     scan      median=%.1f  raw=[ %s ]  CI=[%.1f, %.1f]  per_stmt=%.1f us"
          % (median_scan, " ".join("%.1f" % x for x in scan_ms), scan_low, scan_high,
             median_scan * 1000.0 / TEXT_COUNT))
    print("     filter    median=%.1f  raw=[ %s ]  CI=[%.1f, %.1f]  per_stmt=%.1f us"
          % (median_filter, " ".join("%.1f" % x for x in filter_ms), filter_low, filter_high,
             median_filter * 1000.0 / TEXT_COUNT))
    print("     exec_rest median=%.1f  raw=[ %s ]  CI=[%.1f, %.1f]  per_stmt=%.1f us  "
          "(inside the aggregate path: projection, aggregation, finalise, validation)"
          % (median_rest, " ".join("%.1f" % x for x in rest_ms), rest_low, rest_high,
             median_rest * 1000.0 / TEXT_COUNT))
    print("     outside   median=%.1f  raw=[ %s ]  CI=[%.1f, %.1f]  per_stmt=%.1f us  "
          "(loop minus parse minus the whole aggregate path: dispatch, cache, transaction)"
          % (median_outside, " ".join("%.1f" % x for x in outside_ms), outside_low, outside_high,
             median_outside * 1000.0 / TEXT_COUNT))
    print("     run_parse median=%.1f  raw=[ %s ]  CI=[%.1f, %.1f]  per_stmt=%.1f us  "
          "(parse as the loop pays it; the PARSEONLY leg measures the same texts apart)"
          % (median_loop_parse, " ".join("%.1f" % x for x in loop_parse_ms),
             loop_parse_low, loop_parse_high, median_loop_parse * 1000.0 / TEXT_COUNT))
    print("     exec_wrap median=%.1f  raw=[ %s ]  CI=[%.1f, %.1f]  per_stmt=%.1f us  "
          "(execute() around the body: invalidation check, result handoff)"
          % (median_exec_wrap, " ".join("%.1f" % x for x in exec_wrap_ms),
             exec_wrap_low, exec_wrap_high, median_exec_wrap * 1000.0 / TEXT_COUNT))
    print("     body_rest median=%.1f  raw=[ %s ]  CI=[%.1f, %.1f]  per_stmt=%.1f us  "
          "(statement body outside the aggregate path: dispatch, validation, transaction)"
          % (median_exec_rest2, " ".join("%.1f" % x for x in exec_rest2_ms),
             exec_rest2_low, exec_rest2_high, median_exec_rest2 * 1000.0 / TEXT_COUNT))
    print("     parse_gap median=%.1f  (run_parse - PARSEONLY, per round; this is what the "
          "subtraction used to put inside dispatch)" % statistics.median(gap_ms))
    print("     parse_cache loop hits=%d misses=%d | parse leg hits=%d misses=%d  "
          "(equal by construction: 10k distinct texts through a 1024-entry LRU thrashes)"
          % (cache_legs["loop"][0], cache_legs["loop"][1],
             cache_legs["parse"][0], cache_legs["parse"][1]))
    for state in ("on", "off"):
        if gc_gap[state]:
            print("     gc=%s rounds=%d  residue median=%.1f  (paired loop-parse, same session)"
                  % (state, len(gc_gap[state]), statistics.median(gc_gap[state])))
    # parse_gap's discriminator, frozen before the deciding run: with GC disabled, the gap's
    # per-round median and its seeded-bootstrap 95% interval decide. CI includes zero -> the gap is
    # allocation pressure (collapsed); CI low bound above zero -> real under-load parser cost; any
    # other interval (e.g. wholly negative) -> undetermined, no claim either way.
    if counter_cleared:
        print("     COUNTER INVARIANT VIOLATED: the %s leg cleared the cache mid-leg; its delta is "
              "not a leg count, so no verdict is issued" % ", ".join(sorted(set(counter_cleared))),
              flush=True)
        _gc.enable()
        raise SystemExit(3)
    # Leg balance is its own check, in the same class as the round balance: a verdict computed over
    # fewer paired rounds than N is a thinner claim than the one being advertised.
    loop_legs = cache_legs["loop"][1] // TEXT_COUNT if TEXT_COUNT else 0
    parse_legs = cache_legs["parse"][1] // TEXT_COUNT if TEXT_COUNT else 0
    print("     legs      loop=%d parse=%d of %d rounds  (a parse leg clears the cache, so its totals "
          "are the leg; a lost leg shows here)" % (loop_legs, parse_legs, args.n))
    if args.gc == "alternate" and (loop_legs != args.n or parse_legs != args.n):
        print("     DISCRIMINATOR: undetermined — leg balance failed (loop %d, parse %d, N %d)"
              % (loop_legs, parse_legs, args.n))
    elif args.gc == "alternate" and gc_parse_gap["off"] and gc_parse_gap["on"]:
        if len(gc_parse_gap["off"]) != len(gc_parse_gap["on"]):
            print("     DISCRIMINATOR: undetermined — unequal rounds (%d off vs %d on); rerun with an "
                  "even N so the comparison carries no count asymmetry"
                  % (len(gc_parse_gap["off"]), len(gc_parse_gap["on"])))
        else:
            off_gap = gc_parse_gap["off"]
            low_gap, high_gap = bootstrap_ci(off_gap)
            if low_gap <= 0.0 <= high_gap:
                verdict = "collapsed — CI includes zero, so the gap reads as allocation pressure"
            elif low_gap > 0.0:
                verdict = "real — CI low bound above zero, so the gap survives disabled GC"
            else:
                verdict = "undetermined — CI sits wholly below zero, which is not a case this rule covers"
            print("     parse_gap gc=off median=%.1f CI=[%.1f, %.1f] rounds=%d"
                  % (statistics.median(off_gap), low_gap, high_gap, len(off_gap)))
            print("     DISCRIMINATOR: %s" % verdict)
    _gc.enable()
    print("     plan_path served=aggregate for %d of %d statements (simple-path decisions: %d, "
          "cache_hits=%d cache_misses=%d)"
          % (aggregate_path, TEXT_COUNT * args.n, hits_total + misses_total,
             hits_total, misses_total))

    outside = [x for x in loads if not (WINDOW[0] <= x <= WINDOW[1])]
    if outside or span > DRIFT_SPAN:
        print("INCONCLUSIVE: window/drift not satisfied — outside=%s span=%.2f" % (outside, span))
        return 3
    print("REACHABILITY: " + classify(low, high, args.ceiling))
    return 0


if __name__ == "__main__":
    sys.exit(main())
