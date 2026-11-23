"""Bounded retry loop for the gate-3 baseline: at most 3 attempts of the CAPTURE instrument, not the rig.

Corrected instrument (this matters more than the loop did): a gate-3 baseline is a **capture** --
`tests/skeptic/baseline_g6_quiet.py`, N=7 per series, roughly 30 seconds -- while `gate3_split` is a
decomposition rig whose own docstring says it "issues no pass/fail for goal 6". Running the rig here would
have spent three ten-minute sessions on an instrument that cannot produce a ceiling number, and would have
applied the rig's `span <= 1.0` stability rule to a capture it was never written for.

So this loop:

* records **instrument, D and revision per attempt**, so a survival bound cannot silently inherit another
  instrument's window: the map's 0/34 bound is over 600-second windows, and a 30-second capture has ~1/20th
  the episode exposure. Survival at this D is **unknown**, and the record says so instead of borrowing it.
* applies only the capture's **own** frozen rule -- the comparator match (pre-event reading inside
  [1.0, 3.0] and within 1.0 of the baseline). No span rule, because no rig.
* classifies each start moment with the **ambient map's own vocabulary** (stratum, quiet/busy phase), so the
  refusals arrive pre-labelled for the attribution analysis rather than needing a timestamp join later.
* keeps the frozen cap at 3, publishes the count beside any baseline, and prints **"no baseline obtained;
  refusals recorded"** when all three refuse -- a result about the box and the input the re-baseline
  question needs, not a scheduling verdict.

Usage:
    python tools/baseline_attempts.py --comparator gate3:d89f689+:2.33
    python tools/baseline_attempts.py --simulate 3,3,0 --no-wait      # control flow only, no measurement
"""

import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE = os.path.join(REPO, "tests", "skeptic", "baseline_g6_quiet.py")
INSTRUMENT = "baseline_g6_quiet"
INSTRUMENT_D = 30.0          # seconds, approximate: N=7 series of the tiny fixture
LOG = os.path.expanduser("~/.minidb-baseline-attempts.jsonl")
BASELINE_LOAD = 2.33
BASELINE_DELTA = 1.0
WINDOW = (1.0, 3.0)
MAX_ATTEMPTS = 3
STRATUM_EDGES = (1.0, 1.2, 1.5, 1.8, 2.0)


def read_load():
    with open("/proc/loadavg") as handle:
        return float(handle.read().split()[0])


def comparable(load):
    return WINDOW[0] <= load <= WINDOW[1] and abs(load - BASELINE_LOAD) <= BASELINE_DELTA


def stratum_of(load):
    for edge in STRATUM_EDGES:
        if load <= edge:
            return "<=%.2f" % edge
    return ">%.2f" % STRATUM_EDGES[-1]


def revision():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def wait_for_comparable(cap_seconds, poll=15):
    """Only start an attempt on a comparable reading -- an incomparable start is not a measurement."""
    waited = 0
    while True:
        load = read_load()
        if comparable(load):
            return load, waited
        if waited >= cap_seconds:
            return None, waited
        time.sleep(poll)
        waited += poll


def parse_capture(text):
    values = {}
    for name, pattern in (("loop", r"loop_ms\s+median=([0-9.]+)"),
                          ("parse", r"parse_ms\s+median=([0-9.]+)"),
                          ("reading", r"reading=([0-9.]+)"),
                          ("delta", r"delta=([0-9.]+)")):
        match = re.search(pattern, text)
        if match:
            values[name] = float(match.group(1))
    match = re.search(r"match=(yes|no)", text)
    if match:
        values["match"] = match.group(1) == "yes"
    return values


def run_attempt(number, comparator):
    started = time.time()
    result = subprocess.run([sys.executable, CAPTURE, "--comparator", comparator], cwd=REPO,
                            capture_output=True, text=True)
    text = result.stdout + result.stderr
    record = {"attempt": number, "instrument": INSTRUMENT, "instrument_D_seconds": INSTRUMENT_D,
              "revision": revision(), "exit": result.returncode,
              "seconds": round(time.time() - started, 1), "values": parse_capture(text)}
    refusal = re.search(r"baseline: (.+)", text)
    if refusal:
        record["refusal"] = refusal.group(1).strip()
    return record, text


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    simulate, no_wait, comparator = None, False, None
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
        elif arg == "--comparator" and index + 1 < len(argv):
            comparator = argv[index + 1]
    if comparator is None and simulate is None:
        print("baseline_attempts: --comparator is required (e.g. gate3:d89f689+:2.33) — the capture refuses "
              "to measure against a typed number")
        return 2

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
            record = {"attempt": number, "instrument": INSTRUMENT, "instrument_D_seconds": INSTRUMENT_D,
                      "revision": revision(), "exit": code, "seconds": 0.0, "simulated": True,
                      "values": {}, "refusal": "SIMULATED refusal" if code else ""}
        else:
            record, text = run_attempt(number, comparator)

        record["pre_load"] = round(load, 2)
        record["start_stratum"] = stratum_of(load)
        record["start_phase"] = "quiet" if load <= 2.00 else "busy"
        record["survival_bound"] = ("unknown at D=%.0fs — the map's 0/34 bound is over 600s windows and does "
                                    "not transfer" % INSTRUMENT_D)
        attempts.append(record)
        with open(LOG, "a") as handle:
            handle.write(json.dumps(record) + "\n")

        if record["exit"] == 0:
            baseline = record
            print("attempt %d: ACCEPTED — %s" % (number, record["values"]))
            break
        print("attempt %d: REFUSED (exit %d) — %s" % (number, record["exit"],
                                                      record.get("refusal") or "see trace"))

    print("baseline_attempts  instrument=%s  D~%.0fs  attempts=%d  cap=%d  log=%s"
          % (INSTRUMENT, INSTRUMENT_D, len(attempts), MAX_ATTEMPTS, LOG))
    for record in attempts:
        print("  attempt %d: exit=%d pre_load=%s stratum=%s phase=%s refusal=%s"
              % (record["attempt"], record["exit"], record.get("pre_load"), record.get("start_stratum"),
                 record.get("start_phase"), record.get("refusal") or "-"))
    if baseline is None:
        print("NO BASELINE OBTAINED; REFUSALS RECORDED — a result about the box, and the input the "
              "re-baseline question would need. Not a verdict that the box cannot be scheduled.")
        return 3
    print("BASELINE OBTAINED under %d attempt(s) of the %s capture (D~%.0fs), comparator baseline 2.33: %s"
          % (len(attempts), INSTRUMENT, INSTRUMENT_D, baseline["values"]))
    print("citation label: 'obtained under %d attempts of the %s capture (D~%.0fs), comparator baseline 2.33'"
          " — no causal claim about ambient attaches." % (len(attempts), INSTRUMENT, INSTRUMENT_D))
    return 0


if __name__ == "__main__":
    sys.exit(main())
