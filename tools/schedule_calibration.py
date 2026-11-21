"""Schedule calibration: the tolerance tightens from refusals the schedule actually caused.

The design the room froze, implemented as mechanism with its numbers deliberately absent:

* **Loose start, declared step.** The schedule claims an hourly-share tolerance; a scheduled session
  that refuses for an *ambient* reason tightens it by a frozen step, and the hour that failed is logged.
  Calibration comes from the cost the schedule imposes (a refused session), not from how a percentage
  reads.
* **Closed taxonomy, default = never tighten.** Only reasons that are the box's fault may tighten the
  schedule. An unclassified reason never tightens: a missed tightening costs a slower schedule, while a
  wrong tightening teaches the map from noise it did not cause. The taxonomy extends only by explicit
  ruling, which is why the whitelist lives here as data rather than as a conditional buried in a caller.
* **Operational termination.** The schedule is declared unschedulable when it names hours so narrow no
  session could start inside one — `tolerance × starts_per_cycle < 1`. Without that check the loop
  tightens forever and nobody is rule-triggered to declare the honest outcome.

The three numbers (start tolerance, step, starts per cycle) are NOT defaulted here. They are frozen by
ruling before the first scheduled session, so this module refuses to run without them being supplied
explicitly — a default would be a number chosen for how it reads.

Usage:
    python tools/schedule_calibration.py --tolerance 10 --step 1.5 --starts-per-cycle 6 \\
        --refusal "span/stability" --refusal "dirty tree" --refusal "pre-event comparability"
"""

import argparse
import sys

# Reasons a scheduled session may refuse for where the schedule itself could be at fault. Anything not
# listed here never tightens: the taxonomy is closed by default, exactly as ruled.
TIGHTENING_REASONS = ("span/stability", "pre-event comparability")

# Listed for the record rather than for logic: these are the reasons that must never tighten a schedule,
# and they are already emitted by the instruments that produce them.
NEVER_TIGHTENING_EXAMPLES = ("dirty tree", "counter invariant", "claim level", "provenance",
                             "window guard", "fixture identity")


def classify(reason):
    """'tighten' only for a whitelisted ambient cause; everything else is 'never'."""
    normalised = reason.strip().lower()
    for candidate in TIGHTENING_REASONS:
        if candidate in normalised:
            return "tighten"
    return "never"


def unschedulable(tolerance, starts_per_cycle):
    """The schedule names hours so narrow that no session could launch inside one."""
    return tolerance * starts_per_cycle < 1.0


def calibrate(tolerance, step, starts_per_cycle, refusals):
    """Apply the refusal trail in order; return the trajectory and the terminal state."""
    trail = []
    current = tolerance
    for reason in refusals:
        verdict = classify(reason)
        if verdict == "tighten":
            current = max(0.0, current - step)
            trail.append((reason, "tightened", current))
        else:
            trail.append((reason, "no change (not an ambient cause)", current))
        if unschedulable(current, starts_per_cycle):
            return trail, current, "unschedulable"
    return trail, current, "schedulable"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    tolerance = step = starts_per_cycle = None
    refusals = []
    for index, arg in enumerate(argv):
        if arg == "--tolerance" and index + 1 < len(argv):
            tolerance = float(argv[index + 1])
        elif arg == "--step" and index + 1 < len(argv):
            step = float(argv[index + 1])
        elif arg == "--starts-per-cycle" and index + 1 < len(argv):
            starts_per_cycle = float(argv[index + 1])
        elif arg == "--refusal" and index + 1 < len(argv):
            refusals.append(argv[index + 1])
    missing = [name for name, value in (("--tolerance", tolerance), ("--step", step),
                                        ("--starts-per-cycle", starts_per_cycle)) if value is None]
    if missing:
        print("schedule_calibration: refusing to run without the frozen parameters: %s — they are "
              "frozen by ruling before the first scheduled session, and a default here would be a "
              "number chosen for how it reads" % ", ".join(missing))
        return 2
    print("schedule_calibration  start_tolerance=%.1f  step=%.1f  starts_per_cycle=%.1f  refusals=%d"
          % (tolerance, step, starts_per_cycle, len(refusals)))
    trail, final, state = calibrate(tolerance, step, starts_per_cycle, refusals)
    for reason, action, value in trail:
        print("  refusal=%-28s %-32s tolerance=%.1f" % (reason, action, value))
    print("  final_tolerance=%.1f  state=%s" % (final, state))
    if state == "unschedulable":
        print("  UNSCHEDULABLE — tolerance x starts_per_cycle = %.2f < 1: the schedule names hours so "
              "narrow no session could start inside one. Stop tightening; publish the finding with the "
              "refusal trail as its evidence." % (final * starts_per_cycle))
    return 0


if __name__ == "__main__":
    sys.exit(main())
