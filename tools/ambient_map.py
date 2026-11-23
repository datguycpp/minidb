"""Ambient load map: when can a ten-minute capture hold stationarity on this box?

The problem this answers, measured rather than assumed: `gate3_split` refuses on `span > 1.0` across its
per-round trace, so a session only survives if the box stays flat for its whole length. Five refused
sessions say the box's busy/idle cycle decides that, and "wait for a quiet window" is a coin flip until
the cycle is measured.

The quantity that matters is NOT arithmetic headroom. Under the split admissibility rule, absolute trace
values are context only, so starting lower does not buy margin against the thing that fails the run. What
fails it is an *episode* — a rise of more than the span allowance inside the session's duration. So the
report conditions on the episode probability:

    P(span over the next D seconds > 1.0 | ambient at start <= x)

and the scheduling threshold is whichever x keeps that probability low on this box's own cycle.

Usage:
    python tools/ambient_map.py --sample --interval 5 --log ~/.minidb-ambient.jsonl
    python tools/ambient_map.py --report --log ~/.minidb-ambient.jsonl --duration 600

Sampling appends one JSON line per interval and costs almost nothing: a loadavg read is a file read.
Report mode is pure arithmetic over the log, so any seat can re-derive it from the same file.
"""

import argparse
import json
import math
import os
import sys
import time

DEFAULT_LOG = os.path.expanduser("~/.minidb-ambient.jsonl")
SPAN_ALLOWANCE = 1.0        # the frozen stability rule's allowance, in loadavg
RATE_RULE = "windows-v2"   # both ends observed (tol = 2x interval), censored windows excluded.
                           # windows-v1 counted any window containing samples, so rates computed under it
                           # included truncated windows; occupancy shares counted samples and survive both.
MAX_AGE = 120              # freshness threshold in seconds, frozen in advance: 24x the default 5s
                            # interval. A file that stopped growing is indistinguishable from one nobody
                            # has checked yet, so a rate from a stale log is refused, not caveated.
MIN_WINDOWS = 30            # independent windows, NOT raw starts: at 5s sampling with D=600s, thirty
                            # consecutive starts span 150s -- about a quarter of one window. Counting
                            # starts would let a band print a confident rate from minutes of data.
# D is instrument-specific: episode exposure scales with session length, so scheduling a two-minute
# capture by a ten-minute rig's D would over-restrict it. Frozen per instrument, not globally.
INSTRUMENT_D = {"gate3_split": 600, "retention-coarse": 120, "retention-decision": 700}
THRESHOLDS = (1.0, 1.2, 1.5, 1.8, 2.0, 2.33)


def read_load():
    with open("/proc/loadavg") as handle:
        return float(handle.read().split()[0])


def sample(path, interval):
    while True:
        record = {"t": time.time(), "load": read_load()}
        with open(path, "a") as handle:
            handle.write(json.dumps(record) + "\n")
        time.sleep(interval)


def load_records(path):
    records = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    records.sort(key=lambda item: item["t"])
    return records


def binomial_cdf(k, n, p):
    """P(X <= k) for X ~ Binomial(n, p). Used for the exact one-sided upper bound."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    return sum(math.comb(n, i) * (p ** i) * ((1.0 - p) ** (n - i)) for i in range(0, k + 1))


def clopper_pearson_upper(k, n, confidence=0.95):
    """Exact one-sided upper bound on a rate: the p where P(X <= k; n, p) = 1 - confidence.

    A band with zero observed episodes reports P=0 as a POINT estimate, which is a lower bound wearing
    certainty's clothes; 0 of 30 windows is still consistent with a ~9.5% episode rate, so a scheduling
    decision has to be taken on the bound, not the point.
    """
    if n <= 0:
        return 1.0
    target = 1.0 - confidence
    low, high = 0.0, 1.0
    for _ in range(80):
        mid = (low + high) / 2.0
        if binomial_cdf(k, n, mid) > target:
            low = mid
        else:
            high = mid
    return high


def windows_needed(rate):
    """Windows required for a zero-event band to claim P_upper <= rate (r and floor freeze together)."""
    if rate <= 0.0:
        return float("inf")
    return math.ceil(math.log(0.05) / math.log(1.0 - rate))


def percentile(values, fraction):
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def report(path, duration, synthetic=False, rate_bar=None, max_age=MAX_AGE):
    records = load_records(path)
    if len(records) < 2:
        print("ambient_map: need at least two samples to report (have %d)" % len(records))
        return 2
    age = time.time() - records[-1]["t"]
    stale = age > max_age
    print("  freshness  last_sample=%.0fs ago  max_age=%ds  stale=%s"
          % (age, max_age, "yes" if stale else "no"))
    if stale:
        print("  REFUSED: the log's last sample is older than the freshness threshold, so this file "
              "describes a past window, not the box now. No rate is quoted from a stale log.")
        return 3
    span_seconds = records[-1]["t"] - records[0]["t"]
    gaps = sorted(records[i]["t"] - records[i - 1]["t"] for i in range(1, len(records)))
    interval = gaps[len(gaps) // 2] if gaps else 0.0
    # A log can carry holes (the sampler died once and was restarted 45 h later). Coverage is what the
    # samples actually span, not first-to-last, and a hole must be named rather than averaged into it.
    holes = [gap for gap in gaps if gap > max(2 * interval, max_age)]
    covered = span_seconds - sum(holes)
    loads = [item["load"] for item in records]
    print("ambient_map  samples=%d  covered=%.1f min in %d segment(s)  interval=%.1fs  span_allowance=%.1f"
          "  window=%.0fs"
          % (len(records), covered / 60.0, len(holes) + 1, interval, SPAN_ALLOWANCE, duration))
    print("  rate_rule=%s  tol=%.1fs  (window-level rates die with a window-rule change; occupancy "
          "shares count samples and survive it)" % (RATE_RULE, max(2 * interval, 1.0)))
    if holes:
        print("  coverage holes: %d gap(s) larger than %ds, largest %.1f h — first-to-last span is "
              "%.1f h but only %.1f h of it is sampled"
              % (len(holes), max(2 * interval, max_age), max(holes) / 3600.0, span_seconds / 3600.0,
                 covered / 3600.0))
    print("  source=%s" % ("SYNTHETIC -- metric validation only. The flatness of this table is a "
                          "property of the fixture (episodes placed independently of start ambient), "
                          "not behaviour of the box; only an observed log can test that."
                          if synthetic else "observed (this box's own sampling)"))
    print("  ambient  min=%.2f  p50=%.2f  p90=%.2f  max=%.2f"
          % (min(loads), percentile(loads, 0.5), percentile(loads, 0.9), max(loads)))

    starts = [(index, item["t"], item["load"]) for index, item in enumerate(records)]
    print("  threshold  starts   indep  P_hat   P_upper(95%)   needed_h*")
    for threshold in THRESHOLDS:
        eligible = [(index, load) for index, _, load in starts if load <= threshold]
        if not eligible:
            print("  %-10.2f %-8d %-6s insufficient data" % (threshold, 0, "0"))
            continue
        # Greedy non-overlapping selection: a window that starts within D of the last accepted one is
        # the same observation twice, so it cannot be evidence twice.
        independent, last_t = [], None
        for index, _ in eligible:
            if last_t is None or records[index]["t"] - last_t >= duration:
                independent.append(index)
                last_t = records[index]["t"]
        occupancy = len(eligible) / float(len(records))
        needed_h = (MIN_WINDOWS * duration / 3600.0 / occupancy) if occupancy else float("inf")
        unstable = over = censored_head = censored_tail = 0
        tolerance = max(2 * interval, 1.0)
        for index in independent:
            horizon_end = records[index]["t"] + duration
            observed = [(item["t"], item["load"]) for item in records[index:]
                        if item["t"] <= horizon_end]
            if not observed:
                censored_tail += 1
                continue
            # A window counts only if BOTH ends are observed: a horizon whose tail falls in a gap reads a
            # small span off a truncated fragment and would be recorded stable while the unsampled bulk is
            # unknown. Head check matters for partial gaps -- an empty hole cannot be entered, but a sparse
            # one can, and a guard shaped to a polite hole is a guard shaped to miss the next one.
            if observed[0][0] - records[index]["t"] > tolerance:
                censored_head += 1
                continue
            if observed[-1][0] < horizon_end - tolerance:
                censored_tail += 1
                continue
            values = [value for _, value in observed]
            if max(values) - min(values) > SPAN_ALLOWANCE:
                unstable += 1
            if max(values) > 3.0:
                over += 1
        count = len(independent)
        observed_count = count - censored_head - censored_tail
        point = unstable / observed_count if observed_count else float("nan")
        upper = clopper_pearson_upper(unstable, observed_count) if observed_count else float("nan")
        line = ("  %-10.2f %-8d %-6d %-7s %-14s"
                % (threshold, len(eligible), count,
                   "%.3f" % point if count else "-", "%.3f" % upper if count else "-"))
        if observed_count < MIN_WINDOWS:
            line += (" insufficient (%d observed of %d indep < %d; censored_head=%d censored_tail=%d; "
                     "band %.1f%% of log, needs ~%.0f h)"
                     % (observed_count, count, MIN_WINDOWS, censored_head, censored_tail,
                        occupancy * 100.0, needed_h))
        elif rate_bar is not None:
            line += (" [censored_head=%d censored_tail=%d]" % (censored_head, censored_tail))
            line += (" CLEARS r=%.2f" % rate_bar if upper <= rate_bar
                     else " does not clear r=%.2f (needs ~%d indep)" % (rate_bar,
                                                                       windows_needed(rate_bar)))
        print(line)
    print("  note: P_hat is the point estimate; P_upper(95%) is the exact one-sided Clopper-Pearson "
          "bound. A zero-episode band reads P_hat=0 and P_upper~3/n -- scheduling must use the bound, "
          "because 0 of 30 is still consistent with ~9.5%%, and a threshold r below that cap can never "
          "clear at n=30 (the floor and r freeze together).")
    print("  phase: these rates are measured over the phase(s) the log covers -- see the "
          "phase-of-cycle line below; a quiet-phase rate does not transfer to busy-phase starts.")
    print("  note: P(span) is the stability rule the rig actually fails; P(max>3.0) is context only, "
          "because absolute trace values do not gate under the split rule")
    print("  * needed_h divides by OVERALL occupancy, i.e. it assumes the band's share of the clock is "
          "stationary. On a short log that figure is a projection, not a measurement; the hourly table "
          "below is what turns it into a schedule once the log covers a gateway cycle.")
    if span_seconds >= 2 * 3600:
        buckets = {}
        for item in records:
            hour = time.strftime("%H", time.localtime(item["t"]))
            buckets.setdefault(hour, []).append(item["load"])
        print("  occupancy by hour (share of samples <= 2.00 | median ambient):")
        rows = []
        for hour in sorted(buckets):
            values = buckets[hour]
            share = sum(1 for value in values if value <= 2.0) / float(len(values))
            rows.append("%sh %.0f%%/%.2f" % (hour, share * 100.0, percentile(values, 0.5)))
        for chunk in range(0, len(rows), 6):
            print("    " + "   ".join(rows[chunk:chunk + 6]))
        # Elapsed coverage is not cycle coverage: a schedule's validity depends on the pattern repeating,
        # and the gateway cycle's length is one of the things this map exists to measure. Until the log
        # spans whole days, the table is one partial cycle and says so where it prints.
        if span_seconds < 24 * 3600:
            print("  phase-of-cycle unknown — one partial cycle (%.1f h). Hourly shares describe the "
                  "clock the log covers, not a repeating schedule; they become a schedule only once "
                  "consecutive days can be compared." % (span_seconds / 3600.0))
        else:
            days = {}
            for item in records:
                stamp = time.localtime(item["t"])
                days.setdefault((stamp.tm_yday, stamp.tm_hour), []).append(item["load"])
            pairs = []
            for (day, hour) in list(days):
                previous = days.get((day - 1, hour))
                if previous and days[(day, hour)]:
                    current = days[(day, hour)]
                    pairs.append((hour, sum(1 for v in current if v <= 2.0) / float(len(current)),
                                  sum(1 for v in previous if v <= 2.0) / float(len(previous))))
            if pairs:
                print("  cycle stability: %d same-hour pairs across consecutive days, tolerance NOT yet "
                      "frozen — values reported, no verdict" % len(pairs))
                for hour, now, before in sorted(pairs)[:6]:
                    print("    %sh  today %.0f%%  previous %.0f%%" % (hour, now * 100, before * 100))
            else:
                print("  cycle stability: not evaluable — no same-hour pair across consecutive days")
    else:
        print("  occupancy by hour: not printed — the log covers %.1f min, less than one cycle; the "
              "hourly table appears once it spans two hours or more" % (span_seconds / 60.0))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path, interval, duration = DEFAULT_LOG, 5, 600
    sampling = reporting = synthetic = False
    rate_bar = None
    for index, arg in enumerate(argv):
        if arg == "--sample":
            sampling = True
        elif arg == "--report":
            reporting = True
        elif arg == "--fixture":
            synthetic = True
        elif arg == "--rate-bar" and index + 1 < len(argv):
            rate_bar = float(argv[index + 1])
        elif arg == "--instrument" and index + 1 < len(argv):
            duration = INSTRUMENT_D.get(argv[index + 1], duration)
        elif arg == "--log" and index + 1 < len(argv):
            path = os.path.expanduser(argv[index + 1])
        elif arg == "--interval" and index + 1 < len(argv):
            interval = int(argv[index + 1])
        elif arg == "--duration" and index + 1 < len(argv):
            duration = int(argv[index + 1])
    if sampling:
        sample(path, interval)
        return 0
    if reporting:
        return report(path, duration, synthetic, rate_bar)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
