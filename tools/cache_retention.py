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
SIZES = {"1": 1, "16": 16, "64": 64, "128": 128, "256": 256, "1024": None, "unbounded": 0}
# Frozen constraint: distinct is the open gate, so a size may not regress it by more than this.
DISTINCT_COST_LIMIT = 20.0


REPEAT_SET = 128   # the corpus's own recurring set: 1,759 statements, 1,171 distinct, 128 of them
                   # occur more than once (median 2). A synthetic locality should replay that set.


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def provenance_line(claim):
    """Three facts, because one is a claim about the checkout and not about the code that ran.

    revision alone says nothing about a dirty tree, and a clean tree says nothing about which copy of
    minidb the interpreter imported (a stale install on sys.path would stamp the same hash). So: the
    commit, the worktree state, and the imported package's own location asserted inside the repo --
    the third ported from run_frozen_baseline.py, which solved it.
    """
    import hashlib
    import subprocess

    def run(args):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return ""

    revision = run(["git", "rev-parse", "--short", "HEAD"]).strip() or "unknown"
    status = run(["git", "status", "--porcelain"]).strip()
    dirty = bool(status)
    import minidb
    locations = []
    for module in (minidb,):
        if getattr(module, "__file__", None):
            locations.append(module.__file__)
        else:
            locations.extend(getattr(module, "__path__", []))
    imported_inside = all(os.path.abspath(path).startswith(REPO_ROOT) for path in locations)
    line = "revision=%s dirty=%s modules_in_repo=%s" % (revision, "yes" if dirty else "no",
                                                        "yes" if imported_inside else "no")
    if dirty:
        diff = run(["git", "diff"]).encode()
        line += " diff_sha256=%s" % hashlib.sha256(diff).hexdigest()[:16]
        if claim == "decision":
            raise SystemExit("cache_retention: refusing a decision claim on a dirty tree — %s"
                             % line)
    if not imported_inside:
        raise SystemExit("cache_retention: mixed subject — minidb was imported from outside the "
                         "repo (%s)" % locations)
    return line


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
    settle, settle_cap = 2.60, 120
    for index, arg in enumerate(argv):
        if arg == "--size" and index + 1 < len(argv):
            size = argv[index + 1]
        elif arg == "--n" and index + 1 < len(argv):
            n = int(argv[index + 1])
        elif arg == "--R" and index + 1 < len(argv):
            repeats = int(argv[index + 1])
        elif arg == "--claim" and index + 1 < len(argv):
            claim = argv[index + 1]
        elif arg == "--settle" and index + 1 < len(argv):
            settle = float(argv[index + 1])
        elif arg == "--settle-cap" and index + 1 < len(argv):
            settle_cap = int(argv[index + 1])
    if size not in SIZES and size != "none":
        raise SystemExit("cache_retention: --size takes one of %s or none" % ", ".join(sorted(SIZES)))
    if claim not in ("coarse", "decision"):
        raise SystemExit("cache_retention: --claim takes coarse or decision")
    if n < DEFAULT_N:
        print("cache_retention: N=%d is below the floor of %d — measure only, never cite"
              % (n, DEFAULT_N), file=sys.stderr)

    # Provenance is computed BEFORE the timed work: a refusal after a twenty-minute session is not a
    # refusal, and a decision claim on a dirty tree must die at the door.
    prov = provenance_line(claim)

    distinct_payload = texts("distinct")
    repeat_payload = texts("repeat")
    name, fn, clear = limb_for(size)
    _, none_fn, none_clear = limb_for("none")
    loads = []
    deltas = {"distinct": [], "repeat": []}
    samples = {"distinct": {"none": [], name: []}, "repeat": {"none": [], name: []}}
    def current_load():
        return float(open("/proc/loadavg").read().split()[0])

    for round_index in range(n):
        # The box drifts inside a 90-second session and the window is the precondition, so each
        # round waits for the 1-minute figure to come back under the settle level rather than
        # starting high and calling the result inadmissible afterwards.
        waited = 0
        while settle and current_load() > settle and waited < settle_cap:
            time.sleep(5)
            waited += 5
        loads.append(current_load())
        for shape, payload in (("distinct", distinct_payload), ("repeat", repeat_payload)):
            order = [("none", none_fn, none_clear), (name, fn, clear)]
            if round_index % 2:
                order.reverse()      # round-robin, not blocked halves: a linear drift must cancel
            for limb_name, limb_fn, limb_clear in order:
                samples[shape][limb_name].append(time_pass(limb_fn, limb_clear, payload, repeats))
            deltas[shape].append(samples[shape][name][-1] - samples[shape]["none"][-1])

    print("cache_retention  %s  size=%s" % (prov, size), end="")
    print("  texts=%d  repeat_set=%d  N=%d  R=%d  claim=%s  seed=%d  "
          "resamples=%d"
          % (TEXT_COUNT, REPEAT_SET, n, repeats, claim, BOOTSTRAP_SEED, BOOTSTRAP_RESAMPLES))
    print("loads=[ %s ]" % " ".join("%.2f" % x for x in loads))
    if any(not (1.0 <= x <= 3.0) for x in loads):
        print("  SESSION NOT ADMISSIBLE — a load reading left [1.0, 3.0]; the columns below are "
              "direction-only and cannot be certified")
    for shape in ("distinct", "repeat"):
        low, high = bootstrap_ci(deltas[shape])
        print("  %-8s none median=%.1f | %s median=%.1f | paired delta %+.1f  CI=[%+.1f, %+.1f]  "
              "per-statement=%+.2f us"
              % (shape, statistics.median(samples[shape]["none"]),
                 name, statistics.median(samples[shape][name]),
                 statistics.median(deltas[shape]), low, high,
                 statistics.median(deltas[shape]) * 1000.0 / TEXT_COUNT))

    # The frozen selection rule's constraint, checked where the number is printed: distinct is what
    # gate 3 is judged on, so a size whose distinct cost exceeds the constraint is ineligible no
    # matter how good its repeat column looks. Threshold frozen before the sweep: 20 ms.
    # Three states, not two: a threshold that sits inside its own measurement interval cannot be
    # applied to a point estimate. Eligible only when the WHOLE interval is within the constraint;
    # ineligible only when the whole interval is beyond it; otherwise the honest answer is
    # UNDETERMINED, which never falls back to the median.
    low_d, high_d = bootstrap_ci(deltas["distinct"])
    distinct_cost = statistics.median(deltas["distinct"])
    if high_d <= DISTINCT_COST_LIMIT:
        print("  ELIGIBILITY: ELIGIBLE — distinct CI [%+.1f, %+.1f] wholly within the %.1f ms "
              "constraint" % (low_d, high_d, DISTINCT_COST_LIMIT))
    elif low_d > DISTINCT_COST_LIMIT:
        print("  ELIGIBILITY: INELIGIBLE — distinct CI [%+.1f, %+.1f] wholly beyond the %.1f ms "
              "constraint" % (low_d, high_d, DISTINCT_COST_LIMIT))
    else:
        print("  ELIGIBILITY: UNDETERMINED AT THIS N — distinct %+.1f CI [%+.1f, %+.1f] straddles "
              "the %.1f ms constraint; no eligibility claim, no fallback to the point estimate"
              % (distinct_cost, low_d, high_d, DISTINCT_COST_LIMIT))

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
