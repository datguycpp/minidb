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

import fcntl
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import ambient_map  # noqa: E402
CAPTURE = os.path.join(REPO, "tests", "skeptic", "baseline_g6_quiet.py")
INSTRUMENT = "baseline_g6_quiet"
INSTRUMENT_D = 30.0          # seconds, approximate: N=7 series of the tiny fixture
LOG = os.path.expanduser("~/.minidb-baseline-attempts.jsonl")
AMBIENT = os.path.expanduser("~/.minidb-ambient.jsonl")
# One pair on the box at a time, enforced where the spend happens. The two rows' bands overlap (night
# [0.08, 0.68] and afternoon [0.42, 1.02] share [0.42, 0.68], and both observed quiets sit inside it), so
# two watchers can read `yes` on the same stretch and contaminate each other's loads -- each pair's 30-second
# legs raising the load the other is measured under. A lock in the watcher alone would leave the manual path
# open, so it lives here: flock on an open fd, atomic, self-releasing on crash, no daemon, no cleanup file.
# Acquired non-blocking AFTER the gates pass (never across the pre-launch wait, which would turn one row's
# wait into the other row's starvation) and held across baseline through subject including the inter-leg gap.
LOCK_PATH = os.path.expanduser("~/.minidb-pair.lock")
# The durable pair record. The watcher's stop gate reads THIS, not the registry: the registry says what a
# row is, this says what has already been spent under it. One line per invocation, keyed by the LAUNCH
# identity -- identity-follows-capture means the verdict's own identity is the captured one and would never
# match the pending row a watcher polls, so a stop gate keyed on the verdict identity would silently never
# fire (the unanchored-regex class, wearing a different name).
# NOTE: this file lives alone in the home directory on purpose. A quarantined copy kept beside it with a
# suffixed name (e.g. `.contaminated-by-tests`) would be re-ingested by any future directory-wide read or
# `*.jsonl` glob -- preserving evidence and poisoning the record in the same move. Quarantine goes to
# ~/.minidb-quarantine/, dated, out of the live directory.
PAIR_RECORD = os.environ.get("MINIDB_PAIR_RECORD", os.path.expanduser("~/.minidb-pair-record.jsonl"))
LOCK_EXIT = 5


def acquire_lock(path=LOCK_PATH):
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(handle)
        return None
    return handle


def release_lock(handle):
    if handle is None:
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)
BASELINE_DELTA = 1.0         # comparability for sessions judged against a COMPLETED row
SUBJECT_TOLERANCE = 0.30     # representativeness for the pair's second leg: same load, not merely
                             # same stretch. +-1.0 would admit a baseline at 0.72 and a subject at 1.70 --
                             # the cross-load mix atomicity exists to exclude, behind a gate calling
                             # itself "matched".
WINDOW = (1.0, 3.0)
MAX_ATTEMPTS = 3          # baseline launches, frozen by ruling
AGGREGATE_RULE = "SINGLE_PASS"  # gate 3 is satisfied by ONE complete, properly-gated passing pair: the
                                # comparison is baseline-vs-subject under the SAME conditions, so a pass in
                                # any single mode demonstrates the halving there. Every other completed
                                # verdict is recorded beside it, never averaged or hidden -- a miss in one
                                # mode next to a pass in another is mode-dependent performance, a finding.
                                # Frozen BEFORE any verdict exists: choosing this after two disagreements is
                                # the post-hoc rule-picking the freeze order forbids.
SUBJECT_MAX_ATTEMPTS = 1  # subject launches, counted separately: a baseline-only outcome must not burn
                          # the budget it earned, and subject retries must not be uncapped
STRATUM_EDGES = (1.0, 1.2, 1.5, 1.8, 2.0)


def read_load():
    with open("/proc/loadavg") as handle:
        return float(handle.read().split()[0])


def row_gate(spec):
    """The comparator row decides the pre-gate: a PENDING row gates on its target and launch tolerance,
    a live row on its own base load and the comparability delta. Hard-coded constants here would make the
    new comparator unlaunchable by its own harness -- the exact mismatch the row exists to remove."""
    sys.path.insert(0, os.path.join(REPO, "tests", "skeptic"))
    from comparators import lookup
    entry, problem = lookup(spec)
    if entry is None:
        raise SystemExit("baseline_attempts: unknown comparator %s" % spec)
    if problem is not None and problem[0] == "pending":
        return entry["target_load"], entry["launch_tolerance"], "pending"
    if problem is not None:
        raise SystemExit("baseline_attempts: comparator %s is %s -- name a live or pending row"
                         % (spec, problem[0]))
    return entry["base_load"], BASELINE_DELTA, "live"


def comparable(load, base, tolerance):
    """The window IS the tolerance around the row's own load: [max(0, base - tol), base + tol].

    A frozen absolute window alongside a row-relative match is how the old comparator became
    unlaunchable -- [1.0, 3.0] cannot admit a pending row targeting 0.72, so the derived form is the only
    one that can serve both rows.
    """
    return abs(load - base) <= tolerance


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


def mode_state(base=None, tolerance=None):
    """The mode flag: a precondition for the harness even starting its wait.

    The pointwise gate says the *instant* is admissible; this says the *stretch* is schedulable, and the
    two-gate rule exists because a busy mode can hold a single in-band dip that the pointwise gate would
    wave through. The band is derived from the row the harness was launched with -- a night row targeting
    0.38 must not be judged against the 0.72 row's floor, or the row that names the mode refuses the mode.
    """
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import ambient_map
    if base is None or tolerance is None:
        return ambient_map.trailing_state(AMBIENT)
    return ambient_map.trailing_state(AMBIENT, target=base, band=tolerance)


def wait_for_comparable(cap_seconds, base, tolerance, poll=15):
    """Only start an attempt on a comparable reading -- an incomparable start is not a measurement."""
    waited = 0
    while True:
        load = read_load()
        if comparable(load, base, tolerance):
            return load, waited
        if waited >= cap_seconds:
            return None, waited
        time.sleep(poll)
        waited += poll


def parse_capture(text):
    values = {}
    for name, pattern in (("loop", r"LOOP n=\d+ ms: .*?median=([0-9.]+)"),
                          ("loop_spread", r"LOOP n=\d+ ms: .*?spread=([0-9.]+)%"),
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
    # --series LOOP: the gate-3 baseline is the LOOP median at N=7. Without it the capture also runs
    # BLOCK, whose 100k-row fixture is a gate-1 input and costs minutes for a number gate 3 never reads.
    result = subprocess.run([sys.executable, CAPTURE, "--comparator", comparator, "--series", "LOOP"],
                            cwd=REPO,
                            capture_output=True, text=True)
    text = result.stdout + result.stderr
    record = {"attempt": number, "instrument": INSTRUMENT, "instrument_n": 7,
              "instrument_D_seconds": INSTRUMENT_D,
              "revision": revision(), "exit": result.returncode,
              "seconds": round(time.time() - started, 1), "values": parse_capture(text)}
    refusal = re.search(r"baseline: (.+)", text)
    if refusal:
        record["refusal"] = refusal.group(1).strip()
    return record, text


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    simulate, no_wait, comparator, subject_comparator = None, False, None, None
    wait_cap = 3600
    global LOG, AMBIENT
    for index, arg in enumerate(argv):
        if arg == "--simulate" and index + 1 < len(argv):
            simulate = [int(part) for part in argv[index + 1].split(",")]
        elif arg == "--no-wait":
            no_wait = True
        elif arg == "--wait-cap" and index + 1 < len(argv):
            wait_cap = int(argv[index + 1])
        elif arg == "--ambient" and index + 1 < len(argv):
            AMBIENT = os.path.expanduser(argv[index + 1])
        elif arg == "--log" and index + 1 < len(argv):
            LOG = os.path.expanduser(argv[index + 1])
        elif arg == "--comparator" and index + 1 < len(argv):
            comparator = argv[index + 1]
        elif arg == "--subject-comparator" and index + 1 < len(argv):
            subject_comparator = argv[index + 1]
    if comparator is None and simulate is None:
        print("baseline_attempts: --comparator is required (e.g. gate3:d89f689+:2.33) — the capture refuses "
              "to measure against a typed number")
        return 2

    base, tolerance, row_state = row_gate(comparator) if comparator else (BASELINE_DELTA * 2.33, 1.0, "none")
    row_label = "night" if base < 0.5 else "afternoon"
    print("row gate: comparator=%s state=%s base=%.2f tolerance=%.2f" % (comparator, row_state, base, tolerance))
    state = mode_state(base, tolerance)
    print("mode gate: row-band=[%.2f, %.2f] state=%s share=%.1f%% n=%d (need >=50%% over >=60 samples "
          "before waiting)"
          % (max(0.0, base - tolerance), base + tolerance, state["state"], 100 * state["share"],
             state["n"]))
    if state["state"] != "yes":
        horizon = ambient_map.horizon_status(AMBIENT)
        print("horizon: days_left=%.2f sampler=%s coverage=%.1fh largest_gap=%.1fh%s"
              % (horizon["days_left"], "live" if horizon["live"] else "STALE", horizon["coverage_h"],
                 horizon["largest_gap_h"] or 0.0,
                 "" if not horizon["outcome"] else " -> " + horizon["outcome"]))
        print("baseline_attempts: REFUSED — the box is not in a schedulable mode (%s). The pointwise gate "
              "alone would accept a dip inside an unschedulable stretch, which is exactly the spend the "
              "two-gate rule forbids." % state["state"])
        return 4

    attempts, baseline, subject = [], None, None
    lock_blocked = False
    for number in range(1, MAX_ATTEMPTS + 1):
        if simulate is None:
            load, waited = wait_for_comparable(0 if no_wait else wait_cap, base, tolerance)
            if load is None:
                print("baseline_attempts: no comparable pre-event reading within %ds — not starting an "
                      "attempt that could not be a measurement" % wait_cap)
                break
            print("attempt %d: pre_load=%.2f (comparable, waited %ds)" % (number, load, waited))
        else:
            load = base
            print("attempt %d: SIMULATED at base %.2f" % (number, base))

        # The mode gate is evaluated AGAIN here, at the instant the window is spent. A `yes` at wait-start
        # can flip during a wait of minutes, and the pointwise gate cannot tell a dip from a mode: a gate
        # that is not evaluated where the spend happens is not a gate.
        launch_state = mode_state(base, tolerance)
        if launch_state["state"] != "yes":
            print("attempt %d: ABORTED — mode is %s at launch (was %s at wait-start); the window is not "
                  "spent on a stretch that stopped being schedulable while the harness waited."
                  % (number, launch_state["state"], state["state"]))
            break

        lock = acquire_lock() if simulate is None else os.open(os.devnull, os.O_RDONLY)
        if lock is None:
            print("attempt %d: SKIPPED — another pair holds the box lock (%s); this window is spent by "
                  "someone else and no attempt is consumed." % (number, LOCK_PATH))
            lock_blocked = True
            break

        if simulate is not None:
            code = simulate[min(number - 1, len(simulate) - 1)]
            record = {"attempt": number, "instrument": INSTRUMENT, "instrument_n": 7,
              "instrument_D_seconds": INSTRUMENT_D,
                      "revision": revision(), "exit": code, "seconds": 0.0, "simulated": True,
                      "values": {}, "refusal": "SIMULATED refusal" if code else ""}
        else:
            record, text = run_attempt(number, comparator)

        record["pre_load"] = round(load, 2)
        record["mode_state_at_start"] = state["state"]
        record["mode_state_at_launch"] = launch_state["state"]
        record["mode_share_at_start"] = round(state["share"], 4)
        record["pointwise_in_gate"] = bool(comparable(load, base, tolerance))
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
        release_lock(lock)
        lock = None
        print("attempt %d: REFUSED (exit %d) — %s" % (number, record["exit"],
                                                      record.get("refusal") or "see trace"))

    # Second leg: its own gates, at its own instant. The pair is two spends, and gating only the first
    # makes a "complete" pair whose halves were launched under different rules.
    if baseline is not None and subject_comparator:
        captured = (baseline.get("values") or {}).get("reading", base) or base
        gate = mode_state(captured, tolerance)
        load = captured if simulate is not None else read_load()
        gap = abs(load - captured)
        pointwise = gap <= SUBJECT_TOLERANCE
        print("subject row gate: captured_pre=%.2f reading=%.2f gap=%.2f tolerance=%.2f"
              % (captured, load, gap, SUBJECT_TOLERANCE))
        print("subject leg: mode=%s pointwise=%s pre_load=%.2f" % (gate["state"], "in" if pointwise else "out", load))
        if gate["state"] != "yes" or not pointwise:
            print("subject leg: ABORTED — not launched; the baseline stands (a baseline-only half-pair, "
                  "not a failed attempt). The subject follows in the next matched stretch with the gap "
                  "printed.")
            subject = {"status": "aborted", "mode": gate["state"], "pre_load": round(load, 2),
                       "captured_pre": round(captured, 2), "gap": round(gap, 2),
                       "pointwise_in_gate": pointwise}
        else:
            for attempt in range(1, SUBJECT_MAX_ATTEMPTS + 1):
                if simulate is not None:
                    record = {"attempt": "subject", "exit": 0, "values": {}, "simulated": True,
                              "instrument": INSTRUMENT, "instrument_n": 7, "revision": revision()}
                else:
                    record, text = run_attempt("subject", subject_comparator)
                subject = {"status": "complete" if record["exit"] == 0 else "refused",
                           "attempts": attempt, "mode": gate["state"],
                           "pre_load": round(load, 2), "captured_pre": round(captured, 2),
                           "gap": round(gap, 2), "pointwise_in_gate": True,
                           "exit": record["exit"], "values": record["values"]}
                print("subject leg (attempt %d/%d): %s — %s"
                      % (attempt, SUBJECT_MAX_ATTEMPTS,
                         "ACCEPTED" if record["exit"] == 0 else "REFUSED", record["values"]))
                if record["exit"] == 0:
                    break
    pair_status = ("none" if baseline is None else
                   "complete" if subject and subject.get("status") == "complete" else "baseline-only")
    captured_pre = None
    if baseline is not None:
        captured_pre = ((baseline.get("values") or {}).get("reading")
                        or (baseline.get("pre_load")))
    verdict = {"mode_label": row_label, "aggregate_rule": AGGREGATE_RULE, "status": "undecided"}
    if baseline is not None:
        base_loop = (baseline.get("values") or {}).get("loop")
        subject_loop = ((subject or {}).get("values") or {}).get("loop")
        if base_loop and subject_loop:
            ceiling = base_loop / 2.0
            # The spread is the only robustness information an N=7 gate capture can carry: no interval
            # without a method frozen for it, and a median pass at 2% spread is a different finding from
            # one at 30%. Echoed at birth so the first conditional verdict arrives with its uncertainty.
            verdict.update({"ceiling_ms": round(ceiling, 1), "subject_ms": round(subject_loop, 1),
                            "baseline_spread_pct": (baseline.get("values") or {}).get("loop_spread"),
                            "subject_spread_pct": ((subject or {}).get("values") or {}).get("loop_spread"),
                            "status": "pass" if subject_loop <= ceiling else "miss"})
        else:
            verdict["status"] = "undecided"
    print("verdict  mode=%s aggregate=%s status=%s  %s"
          % (verdict["mode_label"], AGGREGATE_RULE, verdict["status"],
             "" if verdict["status"] == "undecided" else
             "ceiling=%.1f subject=%.1f spreads=%s%%/%s%%"
             % (verdict["ceiling_ms"], verdict["subject_ms"], verdict["baseline_spread_pct"],
                verdict["subject_spread_pct"])))
    print("verdict label for citation: 'gate 3 %s, CONDITIONAL on the %s mode' — not comparable to the "
          "retired 1934.4 or to another mode's verdict without its label."
          % (verdict["status"], verdict["mode_label"]))

    # Durable pair record, keyed by the LAUNCH identity. Identity-follows-capture means the verdict's own
    # identity is the captured one (0.41 here, not 0.38), so a stop gate keyed on the verdict identity would
    # never match the pending row a watcher polls and would never fire -- a gate present in code and dead in
    # effect, the unanchored-regex class under another name.
    try:
        with open(PAIR_RECORD, "a") as handle:
            handle.write(json.dumps({
                "t": time.time(), "rev": revision(), "launch_row": comparator,
                "live_identity": ("gate3:d89f689+:%.2f" % captured_pre) if captured_pre else None,
                "mode_label": verdict["mode_label"], "verdict": verdict.get("status"),
                "pair_status": pair_status, "ceiling_ms": verdict.get("ceiling_ms"),
                "subject_ms": verdict.get("subject_ms"),
                "baseline_spread_pct": verdict.get("baseline_spread_pct"),
                "subject_spread_pct": verdict.get("subject_spread_pct")}) + "\n")
    except OSError as error:
        print("pair record: NOT WRITTEN (%s) — a verdict that cannot be recorded must not be silent" % error)

    release_lock(locals().get("lock"))

    print("baseline_attempts  instrument=%s  D~%.0fs  attempts=%d  cap=%d  pair=%s  log=%s"
          % (INSTRUMENT, INSTRUMENT_D, len(attempts), MAX_ATTEMPTS, pair_status, LOG))
    for record in attempts:
        print("  attempt %d: exit=%d pre_load=%s stratum=%s phase=%s refusal=%s"
              % (record["attempt"], record["exit"], record.get("pre_load"), record.get("start_stratum"),
                 record.get("start_phase"), record.get("refusal") or "-"))
    if lock_blocked and baseline is None:
        print("PAIR SKIPPED — the box lock was held by another pair for the whole window; nothing was "
              "measured and no attempt was consumed.")
        return LOCK_EXIT
    if baseline is None:
        print("NO BASELINE OBTAINED; REFUSALS RECORDED — a result about the box, and the input the "
              "re-baseline question would need. Not a verdict that the box cannot be scheduled.")
        return 3
    print("BASELINE OBTAINED under %d attempt(s) of the %s capture (D~%.0fs), comparator row %s "
          "(base %.2f): %s" % (len(attempts), INSTRUMENT, INSTRUMENT_D, comparator, base,
                               baseline["values"]))
    print("citation label: 'obtained under %d attempts of the %s capture (D~%.0fs), comparator row %s' — "
          "no causal claim about ambient attaches." % (len(attempts), INSTRUMENT, INSTRUMENT_D, comparator))
    return 0


if __name__ == "__main__":
    sys.exit(main())
