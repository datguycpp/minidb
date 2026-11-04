#!/usr/bin/env python3
"""Gate 1's decode block priced on its own: 100k encoded rows, N timed samples (default 7),
both decoders in one frame.

The frozen profile splits gate 1's subject as 35% page walk and 57% record decode. This tool times
the decode half alone -- the same payloads through `record.decode` (the Python reference every
error message and corpus case is pinned against) and through the C accelerator's `decode` -- so a
landing can price the 57% block without pretending to have priced the gate. The page walk is not
in here; a number from this tool is decode-only by construction.

Load is read BEFORE the timed region, per the harness rule the W9 instrument had to learn: a load
captured after the loops describes the box the work left behind.
"""

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb import _native                    # noqa: E402
from minidb.storage import record             # noqa: E402


def loadavg():
    try:
        with open("/proc/loadavg", "r", encoding="ascii") as handle:
            return float(handle.read().split()[0])
    except OSError:
        return None


def build_payloads(rows):
    """The W9 row shape: id int, v int, k text -- encoded once, outside the timed region."""
    payloads = []
    for i in range(1, rows + 1):
        payloads.append(record.encode((i, i % 1000, "k%d" % (i % 100))))
    return payloads


def time_decode(decode, payloads):
    total = 0.0
    for payload in payloads:
        total += len(decode(payload))
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--n", type=int, default=7)
    args = ap.parse_args()

    if _native is None:
        print("DECODE BLOCK: the accelerator is not built; the Python decoder is the reference")
        return 1
    payloads = build_payloads(args.rows)
    check = [payloads[i] for i in range(0, len(payloads), max(1, len(payloads) // 50))]
    for payload in check:
        if _native.decode(payload) != record.decode(payload):
            print("DECODE BLOCK: ABORT -- the decoders disagree before timing")
            return 1

    load_before = loadavg()
    print("DECODE BLOCK  rows=%d  N=%d  module-sqlite n/a" % (args.rows, args.n))
    print("note: decode only -- the page walk is not in this number; ratio = "
          "median(python)/median(native) on unrounded ms.")

    # Warm-up discarded, then N runs per decoder; the harness's order (one side, then the other)
    # is kept so this reads like every other artifact in the record.
    time_decode(record.decode, payloads)
    time_decode(_native.decode, payloads)
    t_py, t_nat = [], []
    for _ in range(args.n):
        start = time.perf_counter()
        time_decode(record.decode, payloads)
        t_py.append((time.perf_counter() - start) * 1000)
    for _ in range(args.n):
        start = time.perf_counter()
        time_decode(_native.decode, payloads)
        t_nat.append((time.perf_counter() - start) * 1000)

    load_after = loadavg()
    print("loadavg=%s  loadavg_after=%s"
          % ("n/a" if load_before is None else "%.2f" % load_before,
             "n/a" if load_after is None else "%.2f" % load_after))
    print("     python_ms median=%.2f  raw=[ %s ]"
          % (statistics.median(t_py), " ".join("%.2f" % x for x in t_py)))
    print("     native_ms median=%.2f  raw=[ %s ]"
          % (statistics.median(t_nat), " ".join("%.2f" % x for x in t_nat)))
    print("     speedup=%.3f" % (statistics.median(t_py) / statistics.median(t_nat)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
