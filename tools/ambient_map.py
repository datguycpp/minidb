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


def report(path, duration):
    records = load_records(path)
    if len(records) < 2:
        print("ambient_map: need at least two samples to report (have %d)" % len(records))
        return 2
    span_seconds = records[-1]["t"] - records[0]["t"]
    loads = [item["load"] for item in records]
    print("ambient_map  samples=%d  covered=%.1f min  interval=%.1fs  span_allowance=%.1f  window=%.0fs"
          % (len(records), span_seconds / 60.0,
             span_seconds / max(1, len(records) - 1), SPAN_ALLOWANCE, duration))
    print("  ambient  min=%.2f  p50=%.2f  p90=%.2f  max=%.2f"
          % (min(loads), percentile(loads, 0.5), percentile(loads, 0.9), max(loads)))

    starts = [(index, item["t"], item["load"]) for index, item in enumerate(records)]
    print("  threshold  starts<=x   P(span>%.1f in %.0fs)   P(max>3.0 in %.0fs)"
          % (SPAN_ALLOWANCE, duration, duration))
    for threshold in THRESHOLDS:
        eligible = [(index, load) for index, _, load in starts if load <= threshold]
        if not eligible:
            print("  %-10.2f %-11d —                       —" % (threshold, 0))
            continue
        unstable = over_ceiling = 0
        for index, _ in eligible:
            horizon_end = records[index]["t"] + duration
            window = [item["load"] for item in records[index:] if item["t"] <= horizon_end]
            if not window:
                continue
            if max(window) - min(window) > SPAN_ALLOWANCE:
                unstable += 1
            if max(window) > 3.0:
                over_ceiling += 1
        total = len(eligible)
        print("  %-10.2f %-11d %-22s %s"
              % (threshold, total, "%.3f (%d/%d)" % (unstable / total, unstable, total),
                 "%.3f" % (over_ceiling / total)))
    print("  note: P(span) is the stability rule the rig actually fails; P(max>3.0) is context only, "
          "because absolute trace values do not gate under the split rule")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path, interval, duration = DEFAULT_LOG, 5, 600
    sampling = reporting = False
    for index, arg in enumerate(argv):
        if arg == "--sample":
            sampling = True
        elif arg == "--report":
            reporting = True
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
        return report(path, duration)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
