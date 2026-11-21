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
import os
import sys
import time

DEFAULT_LOG = os.path.expanduser("~/.minidb-ambient.jsonl")
SPAN_ALLOWANCE = 1.0        # the frozen stability rule's allowance, in loadavg
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


def percentile(values, fraction):
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def report(path, duration, synthetic=False):
    records = load_records(path)
    if len(records) < 2:
        print("ambient_map: need at least two samples to report (have %d)" % len(records))
        return 2
    span_seconds = records[-1]["t"] - records[0]["t"]
    loads = [item["load"] for item in records]
    print("ambient_map  samples=%d  covered=%.1f min  interval=%.1fs  span_allowance=%.1f  window=%.0fs"
          % (len(records), span_seconds / 60.0,
             span_seconds / max(1, len(records) - 1), SPAN_ALLOWANCE, duration))
    print("  source=%s" % ("SYNTHETIC -- metric validation only. The flatness of this table is a "
                          "property of the fixture (episodes placed independently of start ambient), "
                          "not behaviour of the box; only an observed log can test that."
                          if synthetic else "observed (this box's own sampling)"))
    print("  ambient  min=%.2f  p50=%.2f  p90=%.2f  max=%.2f"
          % (min(loads), percentile(loads, 0.5), percentile(loads, 0.9), max(loads)))

    starts = [(index, item["t"], item["load"]) for index, item in enumerate(records)]
    print("  threshold  starts   indep  P(span>%.1f)      needed_h*"
          % SPAN_ALLOWANCE)
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
        if len(independent) < MIN_WINDOWS:
            print("  %-10.2f %-8d %-6d insufficient data (%d indep < floor %d; band occupies %.1f%% of "
                  "the log, needs ~%.0f h)" % (threshold, len(eligible), len(independent),
                                               len(independent), MIN_WINDOWS, occupancy * 100.0,
                                               needed_h))
            continue
        unstable = over = 0
        for index in independent:
            horizon_end = records[index]["t"] + duration
            window = [item["load"] for item in records[index:] if item["t"] <= horizon_end]
            if not window:
                continue
            if max(window) - min(window) > SPAN_ALLOWANCE:
                unstable += 1
            if max(window) > 3.0:
                over += 1
        print("  %-10.2f %-8d %-6d %-15s P(max>3.0)=%.3f"
              % (threshold, len(eligible), len(independent),
                 "%.3f (%d/%d)" % (unstable / len(independent), unstable, len(independent)),
                 over / len(independent)))
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
    else:
        print("  occupancy by hour: not printed — the log covers %.1f min, less than one cycle; the "
              "hourly table appears once it spans two hours or more" % (span_seconds / 60.0))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path, interval, duration = DEFAULT_LOG, 5, 600
    sampling = reporting = synthetic = False
    for index, arg in enumerate(argv):
        if arg == "--sample":
            sampling = True
        elif arg == "--report":
            reporting = True
        elif arg == "--fixture":
            synthetic = True
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
        return report(path, duration, synthetic)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
