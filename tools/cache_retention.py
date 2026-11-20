"""Cache-retention split: does the parse cache cost lookup, retention, or eviction?

Three limbs over the same texts, cache cleared before every pass:
  none        parse() directly        -- no retention, no lookup
  bounded     engine._parse_cached    -- the shipped 1,024-entry LRU (retention + eviction + lookup)
  unbounded   lru_cache(None)(parse)  -- retention, no eviction, same lookup

A previous three-sample run of this shape produced a claim the samples could not carry: bounded vs
none overlapped. This tool is the same experiment at the N the fleet uses, with the seeded percentile
bootstrap of the median, so a difference is quoted only when the intervals separate.

Method: passes alternate limb order so drift lands on all three; every limb's cache is cleared before
its pass; the statistic is the pass median; the interval is a seeded bootstrap of that median.
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


def texts():
    return ["SELECT count(*) FROM t WHERE id > %d" % i for i in range(TEXT_COUNT)]


def bootstrap_ci(values):
    """Seeded percentile bootstrap of the median: the interval is of the statistic being quoted."""
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(values)
    medians = sorted(statistics.median([values[rng.randrange(n)] for _ in range(n)])
                     for _ in range(BOOTSTRAP_RESAMPLES))
    return medians[int(0.025 * BOOTSTRAP_RESAMPLES)], medians[int(0.975 * BOOTSTRAP_RESAMPLES) - 1]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    n = DEFAULT_N
    for index, arg in enumerate(argv):
        if arg == "--n" and index + 1 < len(argv):
            n = int(argv[index + 1])
    if n < DEFAULT_N:
        print("cache_retention: N=%d is below the floor of %d — measure only, never cite"
              % (n, DEFAULT_N), file=sys.stderr)

    payload = texts()
    unbounded = functools.lru_cache(maxsize=None)(parse)
    limbs = [
        ("none", parse, lambda: None),
        ("bounded", engine._parse_cached, engine._parse_cached.cache_clear),
        ("unbounded", unbounded, unbounded.cache_clear),
    ]
    samples = {name: [] for name, _, _ in limbs}
    loads = []
    for round_index in range(n):
        loads.append(float(open("/proc/loadavg").read().split()[0]))
        order = limbs[round_index % 3:] + limbs[:round_index % 3]
        for name, fn, clear in order:
            clear()
            start = time.perf_counter()
            for sql in payload:
                fn(sql)
            samples[name].append((time.perf_counter() - start) * 1000.0)

    print("cache_retention  texts=%d  N=%d  seed=%d  resamples=%d"
          % (TEXT_COUNT, n, BOOTSTRAP_SEED, BOOTSTRAP_RESAMPLES))
    print("loads=[ %s ]" % " ".join("%.2f" % x for x in loads))
    for name, _, _ in limbs:
        values = samples[name]
        low, high = bootstrap_ci(values)
        print("  %-9s median=%.1f  CI=[%.1f, %.1f]  raw=[ %s ]"
              % (name, statistics.median(values), low, high,
                 " ".join("%.1f" % x for x in values)))
    med = {name: statistics.median(values) for name, values in samples.items()}
    ci = {name: bootstrap_ci(values) for name, values in samples.items()}
    for left, right in (("bounded", "none"), ("unbounded", "none"), ("bounded", "unbounded")):
        separated = (ci[left][0] > ci[right][1]) or (ci[right][0] > ci[left][1])
        print("  %s - %s = %+.1f ms  intervals %s"
              % (left, right, med[left] - med[right], "separated" if separated else "OVERLAP"))


if __name__ == "__main__":
    main()
