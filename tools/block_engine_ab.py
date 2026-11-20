"""BLOCK-engine: paired A/B of the engine's decode path, frozen as its own subject (LOG 2984-2991).

Subject: the walk plus `DiskStorage._decode` — the path `Database._rows` already takes. The two legs
differ by **the presence of the compiled `_native` module only**: the python leg sets `disk._native`
to None, the value a failed import leaves, so no worktree and no cross-time comparison enter.

Every leg reports `decoder=native` or `decoder=python` **read back from the branch that actually
served** (call counts on `_native.decode` and `record.decode`), and the run fails if both legs
report the same decoder — a stale `.so` surviving a clean would otherwise manufacture a zero
difference instead of a red run.

Estimator (frozen): interleaved paired A/B, N=11 per leg, alternating which leg goes first so drift
cancels; per-sample load readings; medians and bands reported. Pairing is **positional by round**:
sample i of the native list pairs with sample i of the python list. **Pass** if the paired median
difference is >= 25% of the python-leg median and the 95% CI of that same statistic — a seeded
percentile bootstrap of the median, seed 20261104, 20,000 resamples — excludes zero. The 25% is
derived, not fitted: C decode measured 5.04x on decode-only and decode is ~57% of the block
(<=45% ceiling), so 25% is ~55% of the ceiling.

Exit codes: 0 pass, 2 unusable input, 3 window/drift not satisfied, 4 bar not met.
"""

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.storage import disk, record  # noqa: E402
from minidb.storage.btree import BTree  # noqa: E402
from minidb.storage.pager import Pager  # noqa: E402

PINNED = "f65d581dbef12f634ded07c1e5933fa6558e641dab435718ad29dfbc65d0db82"
BAR = 0.25                 # frozen: >=25% of the python-leg median
BOOTSTRAP_SEED = 20261104  # fixed so the interval is reproducible from the printed raws
BOOTSTRAP_RESAMPLES = 20000
WINDOW = (1.0, 3.0)
DRIFT_SPAN = 1.0


def loadavg():
    try:
        return float(open("/proc/loadavg").read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def band(values):
    median = statistics.median(values)
    return 0.0 if median == 0 else (max(values) - min(values)) / median * 100.0


def bootstrap_ci(values, seed=BOOTSTRAP_SEED, resamples=BOOTSTRAP_RESAMPLES):
    """Percentile bootstrap of the MEDIAN, so the interval belongs to the quoted statistic.

    The freeze pairs a median point estimate with a CI; a t-interval centres on the mean and would
    answer a different question (the seam QA found at the rule's first use). Constants are fixed and
    printed: seed 20261104, 20,000 resamples, 2.5th/97.5th percentiles. Deterministic for a given
    input, so any seat can reproduce the interval from the printed raw samples.
    """
    rng = random.Random(seed)
    n = len(values)
    medians = []
    for _ in range(resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        medians.append(statistics.median(sample))
    medians.sort()
    low = medians[int(0.025 * resamples)]
    high = medians[int(0.975 * resamples) - 1]
    return low, high


def table_root(path, name="t"):
    """Read the catalog through a read-only pager: opening the fixture writable could checkpoint it."""
    pager = Pager(path, readonly=True)
    try:
        tree = BTree(pager)
        for _key, payload in tree.scan(pager.catalog_root):
            entry = json.loads(payload.decode("utf-8"))
            if entry["name"].lower() == name:
                return entry["root"]
    finally:
        pager.close()
    raise SystemExit("block_engine: no table %r in %s" % (name, path))


class Calls:
    """Counts which decoder branch actually served, per leg."""

    def __init__(self):
        self.counts = {}

    def wrap(self, module, attribute, key):
        original = getattr(module, attribute)
        counts = self.counts

        def counted(*args, **kwargs):
            counts[key] = counts.get(key, 0) + 1
            return original(*args, **kwargs)

        setattr(module, attribute, counted)
        return original

    def reset(self):
        self.counts.clear()


def one_leg(path, root, native, calls):
    disk._native = native
    calls.reset()
    pager = Pager(path, readonly=True)
    try:
        tree = BTree(pager)
        started = time.perf_counter()
        rows = 0
        for _rowid, payload in tree.scan(root):
            disk.DiskStorage._decode(payload)
            rows += 1
        elapsed = (time.perf_counter() - started) * 1000.0
    finally:
        pager.close()
    native_calls = calls.counts.get("native", 0)
    python_calls = calls.counts.get("python", 0)
    if native_calls == rows and python_calls == 0:
        served = "native"
    elif python_calls == rows and native_calls == 0:
        served = "python"
    else:
        served = "mixed(%d native, %d python of %d)" % (native_calls, python_calls, rows)
    return elapsed, rows, served


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.environ.get("G6_W9_DB"),
                  os.path.join(repo, "tests", "skeptic", "fixtures", "w9-fixture.db"),
                  os.path.expanduser("~/.hermes/profiles/engineer/cache/scratch/m38/w9-fixture.db")]
    parser.add_argument("--fixture", default=next((c for c in candidates if c and os.path.exists(c)),
                                                 candidates[2]))
    parser.add_argument("--n", type=int, default=11)
    args = parser.parse_args(argv)

    if not os.path.exists(args.fixture):
        print("block_engine: fixture not found: %s" % args.fixture, file=sys.stderr)
        return 2
    digest = sha256(args.fixture)
    if digest != PINNED:
        print("block_engine: fixture identity is %s, not the pinned %s" % (digest, PINNED),
              file=sys.stderr)
        return 2
    if args.n < 11:
        print("block_engine: N=%d is below the frozen floor of 11 — this run can measure, not "
              "certify" % args.n, file=sys.stderr)

    root = table_root(args.fixture)
    calls = Calls()
    original_native = disk._native
    if original_native is None:
        print("block_engine: the native module is not built in this tree — the A/B has no two legs",
              file=sys.stderr)
        return 2
    saved_record_decode = calls.wrap(record, "decode", "python")
    saved_native_decode = original_native.decode
    original_native.decode = _counted_native(calls, saved_native_decode)

    native_ms, python_ms, loads = [], [], []
    served_native, served_python = set(), set()
    try:
        for i in range(args.n):
            native_first = i % 2 == 0
            order = ("native", "python") if native_first else ("python", "native")
            loads.append(loadavg())
            for leg in order:
                value, rows, served = one_leg(args.fixture, root,
                                              original_native if leg == "native" else None, calls)
                (native_ms if leg == "native" else python_ms).append(value)
                (served_native if leg == "native" else served_python).add(served)
    finally:
        disk._native = original_native
        original_native.decode = saved_native_decode
        record.decode = saved_record_decode

    if served_native != {"native"} or served_python != {"python"}:
        print("block_engine: decoder field disagrees — native leg served %s, python leg served %s"
              % (sorted(served_native), sorted(served_python)), file=sys.stderr)
        return 2

    diffs = [p - n for n, p in zip(native_ms, python_ms)]
    median_native = statistics.median(native_ms)
    median_python = statistics.median(python_ms)
    median_diff = statistics.median(diffs)
    share = median_diff / median_python if median_python else 0.0
    mean_diff = statistics.mean(diffs)
    low, high = bootstrap_ci(diffs)

    print("BLOCK-engine  rows=100000  N=%d  fixture_sha256=%s" % (args.n, digest))
    print("loads=[ %s ]  window=[%.1f, %.1f]  span=%.2f"
          % (" ".join("%.2f" % x for x in loads), WINDOW[0], WINDOW[1],
             max(loads) - min(loads)))
    print("     native_ms  median=%.2f  raw=[ %s ]  band=%.1f%%  decoder=native"
          % (median_native, " ".join("%.2f" % x for x in native_ms), band(native_ms)))
    print("     python_ms  median=%.2f  raw=[ %s ]  band=%.1f%%  decoder=python"
          % (median_python, " ".join("%.2f" % x for x in python_ms), band(python_ms)))
    print("     paired_diff median=%.2f ms (%.1f%% of python median)  mean=%.2f (context)"
          % (median_diff, share * 100, mean_diff))
    print("     95%% CI of the median (bootstrap, seed=%d, %d resamples)=[%.2f, %.2f]"
          % (BOOTSTRAP_SEED, BOOTSTRAP_RESAMPLES, low, high))

    outside = [x for x in loads if not (WINDOW[0] <= x <= WINDOW[1])]
    if outside or (max(loads) - min(loads)) > DRIFT_SPAN:
        print("INCONCLUSIVE: window/drift not satisfied — outside=%s span=%.2f"
              % (outside, max(loads) - min(loads)))
        return 3
    passed = share >= BAR and low > 0
    if args.n < 11:
        print("MEASURED ONLY: N=%d is below the frozen floor of 11 — this run can measure, not "
              "certify; it would read %s at %.1f%% (bar 25%%)"
              % (args.n, "PASS" if passed else "MISS", share * 100))
        return 3
    print("VERDICT: %s — bar >=25%% of python median, CI excludes zero; measured %.1f%% CI [%.2f, %.2f]"
          % ("PASS" if passed else "MISS", share * 100, low, high))
    return 0 if passed else 4


def _counted_native(calls, original):
    def counted(payload):
        calls.counts["native"] = calls.counts.get("native", 0) + 1
        return original(payload)
    return counted


if __name__ == "__main__":
    sys.exit(main())
