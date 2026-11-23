"""Unattended watcher: spend a schedulable window when it opens, not when someone notices.

The instrument chain was complete before this existed, and the log's own replay showed the gap: **34
schedulable windows had passed unspent** (11 night, 23 afternoon, the most recent about an hour earlier)
while every manual invocation ran during a `no` and refused. A complete gate nobody runs is a door that
opens only when pushed.

What this does NOT do -- deliberately, because every bound already exists elsewhere:

* it spends nothing on its own account: it calls the frozen pair harness, whose budgets (3 baseline
  attempts, 1 subject), two gates per spend, three-valued pair status and refusal exit codes are unchanged;
* it does not decide schedulability: it reads `ambient_map.trailing_state` for a named row and launches only
  on `yes`, so the 60-sample floor and the trailing-share rule govern it exactly as they govern a human;
* it does not touch rows, ceilings or the record: the harness prints, the JSONL logs, and the verdict carries
  its mode label at birth.

Every poll is one line of JSON in `~/.minidb-pair-watch.jsonl` so the record shows what was seen as well as
what was done -- a watcher that only logged its launches could not be told apart from one that was dead.

Usage:
    python tools/pair_watch.py --comparator gate3:d89f689+:0.38 --dry-run      # prints, launches nothing
    python tools/pair_watch.py --comparator gate3:d89f689+:0.38 --once
    python tools/pair_watch.py --comparator gate3:d89f689+:0.38 --interval 60  # loop (supervise it)
"""

import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import ambient_map  # noqa: E402

AMBIENT = os.path.expanduser("~/.minidb-ambient.jsonl")
WATCH_LOG = os.path.expanduser("~/.minidb-pair-watch.jsonl")
HARNESS = os.path.join(REPO, "tools", "baseline_attempts.py")


def row_band(spec):
    sys.path.insert(0, os.path.join(REPO, "tests", "skeptic"))
    from comparators import lookup
    entry, problem = lookup(spec)
    if entry is None or problem is None or problem[0] != "pending":
        raise SystemExit("pair_watch: %s is not a pending row — the watcher spends on pending rows only" % spec)
    return entry["target_load"], entry["launch_tolerance"]


def record(path, payload):
    with open(path, "a") as handle:
        handle.write(json.dumps(payload) + "\n")


def poll(spec, base, tolerance, dry_run=False, watch_log=WATCH_LOG):
    state = ambient_map.trailing_state(AMBIENT, target=base, band=tolerance)
    entry = {"t": time.time(), "row": spec, "band": [round(max(0.0, base - tolerance), 2),
                                                     round(base + tolerance, 2)],
             "state": state["state"], "share": round(state["share"], 4), "n": state["n"]}
    if state["state"] != "yes":
        record(watch_log, dict(entry, action="wait"))
        return False
    if dry_run:
        record(watch_log, dict(entry, action="would-launch"))
        print("pair_watch: DRY RUN — %s reads yes (share %.1f%%); would launch the pair harness"
              % (spec, 100 * state["share"]))
        return True
    record(watch_log, dict(entry, action="launch"))
    print("pair_watch: %s reads yes (share %.1f%%) — launching the pair harness"
          % (spec, 100 * state["share"]), flush=True)
    result = subprocess.run([sys.executable, HARNESS, "--comparator", spec,
                             "--subject-comparator", spec])
    # Exit 5 is the harness's "another pair holds the box lock": a window that existed and was being spent
    # by someone else. It is a WAIT, not a launch failure -- the distinction the poll log exists to keep.
    if result.returncode == 5:
        record(watch_log, dict(entry, action="wait", reason="lock", t_end=time.time()))
        print("pair_watch: window present but the box lock was held — waiting, nothing spent")
        return False
    record(watch_log, dict(entry, action="launched", exit=result.returncode, t_end=time.time()))
    return result.returncode == 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--comparator", required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", default=WATCH_LOG)
    args = parser.parse_args(argv)

    base, tolerance = row_band(args.comparator)
    print("pair_watch: row=%s band=[%.2f, %.2f] interval=%ds dry_run=%s log=%s"
          % (args.comparator, max(0.0, base - tolerance), base + tolerance, args.interval,
             args.dry_run, args.log), flush=True)
    if args.once or args.dry_run:
        return 0 if poll(args.comparator, base, tolerance, args.dry_run, args.log) else 3
    while True:
        if poll(args.comparator, base, tolerance, False, args.log):
            print("pair_watch: a pair ran; stopping this watcher (one spend per launch by design)")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
