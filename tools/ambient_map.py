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

# Mode table, frozen 2026-11-23 from the ruling before the tool printed anything:
#   TARGET_LOAD is a MODE CENTRE, not a distribution centre. The box is bimodal by day (one day's hours
#   mediate 0.52-0.64, another's 1.53-1.72), so the pooled median (~1.04) names a valley the box never
#   occupies. A comparator is worth measuring where the subject actually runs.
#   MODE_SHARE is definitional rather than tuned: "majority in-gate" IS what mode membership means, and
#   it is deliberately independent of the clock so a diurnal miss still gates launch on an observed mode.
TARGET_LOAD = 0.72
BAND = 0.30
MODE_SHARE = 0.50
# A share computed on a handful of readings is not a share. At 5 s sampling an hour's first minute holds
# 12 samples, where a 50% threshold carries a binomial CI near +/-29 pp -- enough to flag an hour
# schedulable on noise before the mode has spoken. 60 samples (5 min) is the same 12x-interval logic that
# set MAX_AGE = 120 at 24x, and the hourly state is three-valued until it is met rather than guessed.
MODE_MIN_SAMPLES = 60
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


def wilson_ci(k, n, z=1.96):
    """Binomial CI for a share, so a phase-mix figure arrives with its precision."""
    if n == 0:
        return (float("nan"), float("nan"))
    phat = k / float(n)
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def segments_of(records, interval, max_age):
    """Contiguous runs of the series, split at holes -- the unit the phase-mix check compares."""
    segments, current = [], []
    previous = None
    for record in records:
        if previous is not None and record["t"] - previous > max(2 * interval, max_age):
            segments.append(current)
            current = []
        current.append(record)
        previous = record["t"]
    if current:
        segments.append(current)
    return segments


def phase_stretches(records, interval, max_age, delimiter):
    """Contiguous phase stretches under a frozen delimiter.

    'threshold' splits where the 2.00 phase-mix boundary is crossed; 'episode' splits where the span
    allowance is exceeded, i.e. on the map's own episode structure. The two assign windows differently --
    a drift from 1.6 to 2.4 crosses the threshold but may contain no span episode -- which is exactly why
    the choice is a ruling and not a default.
    """
    if not records:
        return []
    bounds = max(2 * interval, max_age)
    stretches = []
    start = records[0]["t"]
    previous = records[0]
    anchor = records[0]["load"]
    for record in records[1:]:
        split = False
        if record["t"] - previous["t"] > bounds:
            split = True
        elif delimiter == "threshold":
            split = (previous["load"] <= 2.00) != (record["load"] <= 2.00)
        else:                                  # 'episode': a span breach restarts the stretch
            split = abs(record["load"] - anchor) > SPAN_ALLOWANCE
        if split:
            stretches.append((start, previous["t"]))
            start = record["t"]
            anchor = record["load"]
        else:
            anchor = min(anchor, record["load"]) if record["load"] < anchor else anchor
        previous = record
    stretches.append((start, records[-1]["t"]))
    return [item for item in stretches if item[1] - item[0] > 0]


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


def report(path, duration, synthetic=False, rate_bar=None, max_age=MAX_AGE, phase_delimiter=None):
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
    # f = the share of samples inside the band: the phase mix that needed_h divides by. Reported with its
    # binomial CI and per segment, because a projection from a partial log is dominated by whichever phase
    # the sample caught -- so whether f is stationary is a question the report should answer, not assume.
    mix_threshold = 2.00
    in_band = sum(1 for value in loads if value <= mix_threshold)
    low_f, high_f = wilson_ci(in_band, len(loads))
    print("  phase mix  f = share of samples <= %.2f:" % mix_threshold)
    print("    pooled  f=%.1f%%  CI[%.1f, %.1f]  n=%d" % (100.0 * in_band / len(loads), 100.0 * low_f,
                                                       100.0 * high_f, len(loads)))
    pieces = segments_of(records, interval, max_age)
    spans = []
    for number, piece in enumerate(pieces, 1):
        hits = sum(1 for item in piece if item["load"] <= mix_threshold)
        low_piece, high_piece = wilson_ci(hits, len(piece))
        spans.append((low_piece, high_piece))
        print("    seg %d   f=%.1f%%  CI[%.1f, %.1f]  n=%d  (%.1f min)"
              % (number, 100.0 * hits / len(piece), 100.0 * low_piece, 100.0 * high_piece,
                 len(piece), (piece[-1]["t"] - piece[0]["t"]) / 60.0))
    if len(spans) > 1:
        disjoint = any(spans[i][1] < spans[j][0] or spans[j][1] < spans[i][0]
                       for i in range(len(spans)) for j in range(i + 1, len(spans)))
        print("    consistency: %s%s" % (
            "DISAGREE (segment CIs disjoint)" if disjoint else "consistent within the CIs",
            " — the phase mix is non-stationary, so needed_h*'s stationary-mix assumption is "
            "undecidable here: two disjoint segments are two PHASE samples, and a short busy segment gives"
            " disjoint CIs even under a perfectly stationary mix -- non-stationarity or phase-sampling,"
            " undecidable until the log spans a cycle"
            if disjoint else ""))
    # Disjoint bands, because the cumulative table above is NESTED: every window counted at start<=1.00 is
    # also counted at start<=2.33, so a difference between those rows is partly composition. A gradient can
    # only be claimed on disjoint strata or a trend statistic.
    edges = [(0.0, 1.0), (1.0, 1.2), (1.2, 1.5), (1.5, 1.8), (1.8, 2.0), (2.0, float("inf"))]
    print("  disjoint bands (start ambient strata, not nested):")
    bins = []
    for low_edge, high_edge in edges:
        members = [(index, load) for index, _, load in starts
                   if low_edge < load <= high_edge or (low_edge == 0.0 and load <= high_edge)]
        if not members:
            continue
        chosen, last_t = [], None
        for index, _ in members:
            if last_t is None or records[index]["t"] - last_t >= duration:
                chosen.append(index)
                last_t = records[index]["t"]
        counted = 0
        unstable = 0
        tolerance = max(2 * interval, 1.0)
        for index in chosen:
            horizon_end = records[index]["t"] + duration
            observed = [(item["t"], item["load"]) for item in records[index:] if item["t"] <= horizon_end]
            if not observed:
                continue
            if observed[0][0] - records[index]["t"] > tolerance:
                continue
            if observed[-1][0] < horizon_end - tolerance:
                continue
            counted += 1
            values = [value for _, value in observed]
            if max(values) - min(values) > SPAN_ALLOWANCE:
                unstable += 1
        label = "%s-%s" % ("0" if low_edge == 0.0 else "%.2f" % low_edge,
                           "inf" if high_edge == float("inf") else "%.2f" % high_edge)
        if counted:
            print("    %-12s indep=%-4d events=%-3d P_hat=%.3f  P_upper=%.3f"
                  % (label, counted, unstable, unstable / counted,
                     clopper_pearson_upper(unstable, counted)))
        else:
            print("    %-12s indep=0" % label)
        bins.append((counted, unstable, (low_edge + (2.0 if high_edge == float("inf") else high_edge)) / 2.0))
    usable = [(n, k, score) for n, k, score in bins if n > 0]
    total_n = sum(n for n, _, _ in usable)
    total_k = sum(k for _, k, _ in usable)
    if total_n > 1 and 0 < total_k < total_n:
        pbar = total_k / float(total_n)
        weighted = sum(score * n for n, _, score in usable)
        numerator = sum(score * (k - n * pbar) for n, k, score in usable)
        variance = pbar * (1 - pbar) * (sum(n * score * score for n, _, score in usable)
                                        - weighted * weighted / total_n)
        if variance > 0:
            z_value = numerator / math.sqrt(variance)
            p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(z_value) / math.sqrt(2))))
            print("    trend (Cochran-Armitage on disjoint strata): z=%.2f  p=%.3f  %s"
                  % (z_value, p_value, "gradient supported" if p_value < 0.05
                     else "no trend detected at this n"))
            print("    score convention: strata scored at their lower edge; the open top stratum [2.00, inf) "
                  "is scored 2.0 because its midpoint is unbounded. A different score changes |z|, not the "
                  "direction at these event rates -- the magnitude is convention-conditioned and every "
                  "citation must say so.")
        else:
            print("    trend: not computable (zero variance)")
    else:
        print("    trend: not computable yet (needs events and non-events across strata)")
    # Within-phase attribution: does the ambient gradient survive inside a single phase stretch, or is
    # ambient a proxy for the box being in a busy phase? The delimiter is DELIBERATELY not defaulted --
    # two candidate rules already exist on the board (contiguous runs bounded by span-episode structure,
    # and the fixed 2.00 phase-mix boundary) and they assign windows differently, so choosing after seeing
    # the result would be a plausibility constant. The block refuses until one is frozen by ruling.
    if phase_delimiter is None:
        print("  within-phase attribution: NOT COMPUTED — phase delimiter not frozen. Pass "
              "--phase-delimiter episode|threshold once the room rules which one governs; the two assign "
              "windows differently and the gradient may move with the choice.")
    else:
        phases = phase_stretches(records, interval, max_age, phase_delimiter)
        print("  within-phase attribution (delimiter=%s, %d phase stretch(es)):" % (phase_delimiter,
                                                                                  len(phases)))
        for number, (phase_start, phase_end) in enumerate(phases, 1):
            inner = [(index, load) for index, _, load in starts
                     if phase_start <= records[index]["t"] <= phase_end]
            chosen, last_t = [], None
            for index, _ in inner:
                if last_t is None or records[index]["t"] - last_t >= duration:
                    chosen.append(index)
                    last_t = records[index]["t"]
            counted = unstable = 0
            low_stratum = high_stratum = low_events = high_events = 0
            tolerance = max(2 * interval, 1.0)
            for index in chosen:
                horizon_end = records[index]["t"] + duration
                observed = [(item["t"], item["load"]) for item in records[index:] if item["t"] <= horizon_end]
                if not observed:
                    continue
                if observed[0][0] - records[index]["t"] > tolerance:
                    continue
                if observed[-1][0] < horizon_end - tolerance:
                    continue
                counted += 1
                event = max(value for _, value in observed) - min(value for _, value in observed) > SPAN_ALLOWANCE
                unstable += 1 if event else 0
                if records[index]["load"] <= 1.5:
                    low_stratum += 1
                    low_events += 1 if event else 0
                else:
                    high_stratum += 1
                    high_events += 1 if event else 0
            span_min = (phase_end - phase_start) / 60.0
            if counted < 6 or low_stratum < 2 or high_stratum < 2:
                print("    phase %d (%.0f min): insufficient per phase at this N — windows=%d "
                      "strata(<=1.50:%d, >1.50:%d); more quiet-phase accumulation, not a reinterpretation"
                      % (number, span_min, counted, low_stratum, high_stratum))
            else:
                rate_low = low_events / float(low_stratum)
                rate_high = high_events / float(high_stratum)
                print("    phase %d (%.0f min): windows=%d  P(<=1.50)=%.2f (%d/%d)  P(>1.50)=%.2f (%d/%d)  %s"
                      % (number, span_min, counted, rate_low, low_events, low_stratum, rate_high,
                         high_events, high_stratum,
                         "gradient PERSISTS within phase" if rate_high - rate_low >= 0.3
                         else "gradient vanishes within phase (ambient looks like a phase proxy)"))
        print("    note: cells are sparse by construction at this N; 'insufficient per phase' is the "
              "pre-registered most-likely outcome and is not the proxy answer.")

    print("  note: the CUMULATIVE rows above are nested samples, so their difference is partly composition;"
          " a gradient claim needs these disjoint strata or the trend line, and both inherit the phase"
          " scope -- phase-scoped until a full cycle is covered.")
    print("  note: P_hat is the point estimate; P_upper(95%) is the exact one-sided Clopper-Pearson "
          "bound. A zero-episode band reads P_hat=0 and P_upper~3/n -- scheduling must use the bound, "
          "because 0 of 30 is still consistent with ~9.5%, and a threshold r below that cap can never "
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


def trailing_state(path, target=TARGET_LOAD, band=BAND, share=MODE_SHARE, minimum=MODE_MIN_SAMPLES,
                   max_age=MAX_AGE):
    """Trailing window over the most recent samples: is this stretch schedulable?

    The table's per-hour share answers "which hours are usually quiet"; this answers "is the box in the
    quiet mode *now*". Both are needed because neither implies the other: a busy hour can hold a single
    in-band dip, and the pointwise reading gate would wave that dip through while the mode is unschedulable.
    """
    records = load_records(path)
    if not records:
        return {"state": "insufficient", "n": 0, "share": 0.0, "age": None}
    age = time.time() - records[-1]["t"]
    if age > max_age:
        return {"state": "stale", "n": len(records), "share": 0.0, "age": age}
    window = records[-minimum:]
    low, high = max(0.0, target - band), target + band
    inside = sum(1 for record in window if low <= record["load"] <= high)
    fraction = inside / len(window) if window else 0.0
    if len(window) < minimum:
        state = "insufficient"
    else:
        state = "yes" if fraction >= share else "no"
    return {"state": state, "n": len(window), "share": fraction, "age": age}


def modes(path, target=TARGET_LOAD, band=BAND, share=MODE_SHARE, max_age=MAX_AGE):
    """Per-day per-hour p50 and in-gate share, plus the valley line.

    The in-gate share per hour IS the schedulability metric: a launch needs an observed quiet mode, not a
    clock. Prints nothing it did not count, and refuses a stale log like every other report.
    """
    import datetime

    records = load_records(path)
    if not records:
        print("ambient_modes: no records in %s" % path)
        return 3
    age = time.time() - records[-1]["t"]
    stale = age > max_age
    low, high = max(0.0, target - band), target + band
    print("ambient_modes  n=%d  target=%.2f (MODE CENTRE)  band=[%.2f, %.2f]  "
          "mode_share>=%.2f  min_samples=%d  stale=%s (%.0fs old)"
          % (len(records), target, low, high,
             share, MODE_MIN_SAMPLES, "yes" if stale else "no", age))
    if stale:
        print("ambient_modes: REFUSED — log is stale (%.0fs > %ds); no schedule is read from a stale log"
              % (age, max_age))
        return 3

    buckets = {}
    for record in records:
        stamp = datetime.datetime.fromtimestamp(record["t"])
        buckets.setdefault((stamp.strftime("%Y-%m-%d"), stamp.hour), []).append(record["load"])
    days = {}
    print("day        hour      n   p50   in_gate   schedulable")
    for (day, hour) in sorted(buckets):
        loads = buckets[(day, hour)]
        inside = sum(1 for value in loads if low <= value <= high)
        fraction = inside / len(loads)
        days.setdefault(day, []).extend(loads)
        if len(loads) < MODE_MIN_SAMPLES:
            state = "insufficient samples"
        else:
            state = "yes" if fraction >= share else "no"
        print("%s  %02d:00  %5d  %5.2f   %5.1f%%    %s"
              % (day, hour, len(loads), sorted(loads)[len(loads) // 2], 100 * fraction, state))
    medians = {day: sorted(v)[len(v) // 2] for day, v in days.items()}
    print("day medians: " + "  ".join("%s=%.2f(n=%d)" % (day, medians[day], len(days[day]))
                                      for day in sorted(medians)))
    if len(medians) >= 2:
        spread = max(medians.values()) - min(medians.values())
        if spread > band:
            print("valley: day medians differ by %.2f (> band %.2f) — two modes, and the pooled centre "
                  "between them names no state the box occupies; target is a MODE CENTRE" % (spread, band))
        else:
            print("valley: none — day medians differ by %.2f, inside one band, so the days are one mode"
                  % spread)
    else:
        print("cycle stability: not evaluable — fewer than two day medians in the log")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path, interval, duration = DEFAULT_LOG, 5, 600
    sampling = reporting = synthetic = modes_flag = False
    rate_bar = None
    phase_delimiter = None
    for index, arg in enumerate(argv):
        if arg == "--sample":
            sampling = True
        elif arg == "--report":
            reporting = True
        elif arg == "--fixture":
            synthetic = True
        elif arg == "--modes":
            modes_flag = True
        elif arg == "--rate-bar" and index + 1 < len(argv):
            rate_bar = float(argv[index + 1])
        elif arg == "--phase-delimiter" and index + 1 < len(argv):
            phase_delimiter = argv[index + 1]
            if phase_delimiter not in ("episode", "threshold"):
                raise SystemExit("ambient_map: --phase-delimiter takes episode or threshold")
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
        return report(path, duration, synthetic, rate_bar, MAX_AGE, phase_delimiter)
    if modes_flag:
        return modes(path)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
