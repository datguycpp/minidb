"""Bounded retry loop for the gate-3 parse baseline: at most 3 attempts, every refusal recorded.

The frozen policy this implements:

* a maximum of **3 attempts**, each started only on a **comparable pre-event reading** (inside [1.0, 3.0]
  and within 1.0 of the 2.33 baseline);
* every refusal recorded with its trace and its reason, because refused attempts are the policy's **data**,
  not its failure count;
* the attempt count published beside any resulting baseline, and any number labelled
  "obtained under N attempts, comparator baseline 2.33, span-verified" with no causal claim about ambient;
* if all three refuse, the output is **"no baseline obtained; refusals recorded"** — a result about the box,
  which is what the re-baseline question would need.

The pre-registered expectation, written before the attempts: the comparable band is where episodes are
near-certain (0 of 34 comparable-start windows survived in the map's own record), so "no baseline obtained"
is the likely outcome and an underpowered refusal is not a scheduling verdict.

Usage:
    python tools/baseline_attempts.py                 # real: runs gate3_split up to 3 times
    python tools/baseline_attempts.py --simulate 3,3,3 --no-wait   # control-flow check, no measurement
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE3 = os.path.join(REPO, "tools", "gate3_split.py")
LOG = os.path.expanduser("~/.minidb-baseline-attempts.jsonl")
BASELINE_LOAD = 2.33
BASELINE_DELTA = 1.0
WINDOW = (1.0, 3.0)
MAX_ATTEMPTS = 3


def read_load():
    with open("/proc/loadavg") as handle:
        return float(handle.read().split()[0])


def comparable(load):
    return WINDOW[0] <= load <= WINDOW[1] and abs(load - BASELINE_LOAD) <= BASELINE_DELTA


def wait_for_comparable(cap_seconds, poll=30):
    """Only start an attempt on a comparable reading -- an incomparable start would not be a measurement."""
    waited = 0
    while True:
        load = read_load()
        if comparable(load):
            return load, waited
        if waited >= cap_seconds:
            return None, waited
        time.sleep(poll)
        waited += poll


def parse_medians(text):
    values = {}
    for name, pattern in (("loop", r"loop_ms\s+median=([0-9.]+)"), ("parse", r"parse_ms\s+median=([0-9.]+)"),
                          ("residue", r"residue\s+median=([0-9.]+)"),
                          ("span", r"trace_span=([0-9.]+)")):
        match = re.search(pattern, text)
        if match:
            values[name] = float(match.group(1))
    return values


def run_attempt(number):
    started = time.time()
    result = subprocess.run([sys.executable, GATE3], capture_output=True, text=True)
    text = result.stdout + result.stderr
    record = {"attempt": number, "exit": result.returncode, "started": started,
              "seconds": round(time.time() - started, 1), "medians": parse_medians(text)}
    refusal = re.search(r"INCONCLUSIVE: (.+)", text)
    if refusal:
        record["refusal"] = refusal.group(1).strip()
    return record, text


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    simulate, no_wait = None, False
    wait_cap = 3600
    global LOG
    for index, arg in enumerate(argv):
        if arg == "--simulate" and index + 1 < len(argv):
            simulate = [int(part) for part in argv[index + 1].split(",")]
        elif arg == "--no-wait":
            no_wait = True
        elif arg == "--wait-cap" and index + 1 < len(argv):
            wait_cap = int(argv[index + 1])
        elif arg == "--log" and index + 1 < len(argv):
            LOG = os.path.expanduser(argv[index + 1])

    attempts, baseline = [], None
    for number in range(1, MAX_ATTEMPTS + 1):
        if simulate is None:
            load, waited = wait_for_comparable(0 if no_wait else wait_cap)
            if load is None:
                print("baseline_attempts: no comparable pre-event reading within %ds — not starting an "
                      "attempt that could not be a measurement" % wait_cap)
                break
            print("attempt %d: pre_load=%.2f (comparable, waited %ds)" % (number, load, waited))
        else:
            load = BASELINE_LOAD
            print("attempt %d: SIMULATED" % number)

        if simulate is not None:
            code = simulate[min(number - 1, len(simulate) - 1)]
            text = "INCONCLUSIVE: trace span 99.0 exceeds 1.0" if code == 3 else "REACHABILITY: excluded"
            record = {"attempt": number, "exit": code, "seconds": 0.0, "simulated": True,
                      "medians": {}, "refusal": "trace span 99.0 exceeds 1.0" if code == 3 else ""}
        else:
            record, text = run_attempt(number)
        record["pre_load"] = round(load, 2)
        attempts.append(record)
        with open(LOG, "a") as handle:
            handle.write(json.dumps(record) + "\n")

        if record["exit"] == 0:
            baseline = record
            print("attempt %d: ACCEPTED — %s" % (number, record["medians"]))
            break
        print("attempt %d: REFUSED (exit %d) — %s" % (number, record["exit"],
                                                      record.get("refusal") or "see trace"))

    print("baseline_attempts  attempts=%d  cap=%d  log=%s" % (len(attempts), MAX_ATTEMPTS, LOG))
    for record in attempts:
        print("  attempt %d: exit=%d pre_load=%s refusal=%s" % (record["attempt"], record["exit"],
                                                               record.get("pre_load"),
                                                               record.get("refusal") or "-"))
    if baseline is None:
        print("NO BASELINE OBTAINED; REFUSALS RECORDED — this is a result about the box, and the input the "
              "re-baseline question would need. It is not a verdict that the box cannot be scheduled.")
        return 3
    print("BASELINE OBTAINED under %d attempt(s), comparator baseline 2.33, span-verified: %s"
          % (len(attempts), baseline["medians"]))
    print("label for citation: 'obtained under %d attempts, comparator baseline 2.33, span-verified' — no "
          "causal claim about ambient attaches to this number." % len(attempts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
