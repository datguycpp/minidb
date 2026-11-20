"""Cache-retention sweep, staged: one size per session, paired against no-cache in the same session.

Why staged: a six-size sweep at the power the decision needs costs ~58 minutes of continuous timing,
and this box drifts out of the frozen [1.0, 3.0] window inside shorter sessions than that. So each
session measures ONE candidate size against `none`, interleaved — the paired discipline that made the
parse residue trustworthy, since drift then moves both limbs together.

Claim levels are structural, not editorial:
  --claim coarse     direction-only: the delta and its interval are printed, and separation wording is
                     REFUSED. A knee located at this power is a place to look, never a decision.
  --claim decision   the high-power A/B at the knee (R=20): separation may be claimed, and only when
                     the paired delta's bootstrapped interval excludes zero.

Every pass alternates which limb runs first, round-robin, so a linear drift across the session cannot
bias the delta the pairing exists to cancel.
"""

import functools
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb import engine
from minidb.sql.parser import parse

TEXT_COUNT = 10000
DEFAULT_N = 11
BOOTSTRAP_SEED = 20261104
BOOTSTRAP_RESAMPLES = 20000
SIZES = {"1": 1, "16": 16, "64": 64, "256": 256, "1024": None, "unbounded": 0}


REPEAT_SET = 128   # the corpus's own recurring set: 1,759 statements, 1,171 distinct, 128 of them
                   # occur more than once (median 2). A synthetic locality should replay that set.


def texts(shape="distinct", repeat_set=REPEAT_SET):
    """The two shapes the policy trades between.

    distinct: the gate-3 workload -- every statement unique, so every cache size has zero hits and
              its only effect is retention plus wrapper cost.
    repeat:   the corpus's recurrence -- a small set replayed, so a cache can actually hit.
    """
    if shape == "distinct":
        return ["SELECT count(*) FROM t WHERE id > %d" % i for i in range(TEXT_COUNT)]
    base = ["SELECT count(*) FROM t WHERE id > %d" % i for i in range(repeat_set)]
    return [base[i % repeat_set] for i in range(TEXT_COUNT)]


def bootstrap_ci(values):
    """Seeded percentile bootstrap of the median: the interval is of the statistic being quoted."""
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(values)
    medians = sorted(statistics.median([values[rng.randrange(n)] for _ in range(n)])
                     for _ in range(BOOTSTRAP_RESAMPLES))
    return medians[int(0.025 * BOOTSTRAP_RESAMPLES)], medians[int(0.975 * BOOTSTRAP_RESAMPLES) - 1]


def limb_for(size):
    """(name, callable, clear) for one cache size; 'none' is no cache at all."""
    if size == "none":
        return "none", parse, lambda: None
    if size == "1024":
        return "1024", engine._parse_cached, engine._parse_cached.cache_clear
    if size == "unbounded":
        cached = functools.lru_cache(maxsize=None)(parse)
        return "unbounded", cached, cached.cache_clear
    cached = functools.lru_cache(maxsize=SIZES[size])(parse)
    return size, cached, cached.cache_clear


def time_pass(fn, clear, payload, repeats):
    clear()
    start = time.perf_counter()
    for _ in range(repeats):
        for sql in payload:
            fn(sql)
    return (time.perf_counter() - start) * 1000.0 / repeats


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    size, n, repeats, claim = "1024", DEFAULT_N, 5, "coarse"
    for index, arg in enumerate(argv):
        if arg == "--size" and index + 1 < len(argv):
            size = argv[index + 1]
        elif arg == "--n" and index + 1 < len(argv):
            n = int(argv[index + 1])
        elif arg == "--R" and index + 1 < len(argv):
            repeats = int(argv[index + 1])
        elif arg == "--claim" and index + 1 < len(argv):
            claim = argv[index + 1]
    if size not in SIZES and size != "none":
        raise SystemExit("cache_retention: --size takes one of %s or none" % ", ".join(sorted(SIZES)))
    if claim not in ("coarse", "decision"):
        raise SystemExit("cache_retention: --claim takes coarse or decision")
    if n < DEFAULT_N:
        print("cache_retention: N=%d is below the floor of %d — measure only, never cite"
              % (n, DEFAULT_N), file=sys.stderr)

    distinct_payload = texts("distinct")
    repeat_payload = texts("repeat")
    name, fn, clear = limb_for(size)
    _, none_fn, none_clear = limb_for("none")
    loads = []
    deltas = {"distinct": [], "repeat": []}
    samples = {"distinct": {"none": [], name: []}, "repeat": {"none": [], name: []}}
    for round_index in range(n):
        loads.append(float(open("/proc/loadavg").read().split()[0]))
        for shape, payload in (("distinct", distinct_payload), ("repeat", repeat_payload)):
            order = [("none", none_fn, none_clear), (name, fn, clear)]
            if round_index % 2:
                order.reverse()      # round-robin, not blocked halves: a linear drift must cancel
            for limb_name, limb_fn, limb_clear in order:
                samples[shape][limb_name].append(time_pass(limb_fn, limb_clear, payload, repeats))
            deltas[shape].append(samples[shape][name][-1] - samples[shape]["none"][-1])

    print("cache_retention  size=%s  texts=%d  repeat_set=%d  N=%d  R=%d  claim=%s  seed=%d  "
          "resamples=%d"
          % (size, TEXT_COUNT, REPEAT_SET, n, repeats, claim, BOOTSTRAP_SEED, BOOTSTRAP_RESAMPLES))
    print("loads=[ %s ]" % " ".join("%.2f" % x for x in loads))
    for shape in ("distinct", "repeat"):
        low, high = bootstrap_ci(deltas[shape])
        print("  %-8s none median=%.1f | %s median=%.1f | paired delta %+.1f  CI=[%+.1f, %+.1f]  "
              "per-statement=%+.2f us"
              % (shape, statistics.median(samples[shape]["none"]),
                 name, statistics.median(samples[shape][name]),
                 statistics.median(deltas[shape]), low, high,
                 statistics.median(deltas[shape]) * 1000.0 / TEXT_COUNT))

    separated = any(bootstrap_ci(deltas[shape])[0] > 0.0 or bootstrap_ci(deltas[shape])[1] < 0.0
                    for shape in ("distinct", "repeat"))
    if claim == "coarse":
        print("  CLAIM LEVEL: DIRECTION ONLY — no separation claim at this power; the delta locates "
              "a knee to test, it does not decide a policy")
    elif separated:
        print("  CLAIM LEVEL: SEPARATED — the paired interval excludes zero")
    else:
        print("  CLAIM LEVEL: NOT SEPARATED — the paired interval includes zero; no policy decision "
              "follows from this session")


if __name__ == "__main__":
    main()
