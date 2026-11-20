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


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0] or "")
    parser.add_argument("--n", type=int, default=11)
    parser.add_argument("--ceiling", type=float, default=CEILING)
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
     outside_ms, loads) = [], [], [], [], [], [], [], [], []
    aggregate_path = 0
    hits_total = misses_total = 0
    for i in range(args.n):
        loads.append(loadavg())
        probe = {"prep_ns": 0, "scan_ns": 0, "filter_ns": 0, "complex_ns": 0, "aggregate": 0,
                 "simple_ns": 0, "simple_hits": 0, "simple_misses": 0}
        parse_first = i % 2 == 0
        if parse_first:
            parse_ms.append(timed(parse_only))
            loop_ms.append(timed(lambda: loop(probe)))
        else:
            loop_ms.append(timed(lambda: loop(probe)))
            parse_ms.append(timed(parse_only))
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
    median_residue = statistics.median(residue)
    low, high = bootstrap_ci(residue)
    prep_low, prep_high = bootstrap_ci(prep_ms)
    scan_low, scan_high = bootstrap_ci(scan_ms)
    rest_low, rest_high = bootstrap_ci(rest_ms)
    filter_low, filter_high = bootstrap_ci(filter_ms)
    outside_low, outside_high = bootstrap_ci(outside_ms)
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
