#!/usr/bin/env python3
"""`make verify` — the one command that answers "is this tree good?".

Four required stages, each with its own raw counts:

  build   builds the optional C accelerator (a clone has no .so; the package runs without
          it, so a missing compiler is SKIP, not FAIL)
  tests   the unit suite
  corpus  the differential corpus against the pinned SQLite binary
  crash   the seeded crash sweep (real processes killed mid-write, then invariants)
  docs    every executable block in README.md and docs/*.md

One summary block at the end, always printed, always with raw counts and the git
revision. A failing stage prints FAIL, keeps its counts, and makes the command exit
non-zero; its raw output tail is reproduced below the table rather than absorbed.
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def revision():
    try:
        head = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown", 0
    dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                           capture_output=True, text=True).stdout.splitlines()
    return head, len(dirty)


def loadavg():
    try:
        with open("/proc/loadavg", "r", encoding="ascii") as handle:
            return handle.read().split()[0]
    except (OSError, ValueError, IndexError):
        return "n/a"


def run(command, timeout=1800):
    started = time.perf_counter()
    try:
        finished = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                  timeout=timeout)
        output = (finished.stdout or "") + (finished.stderr or "")
        code = finished.returncode
    except subprocess.TimeoutExpired as expired:
        output = ((expired.stdout or "") if isinstance(expired.stdout, str) else "") + \
                 "\n[verify] timeout after %ss" % timeout
        code = 124
    return code, output, time.perf_counter() - started


def build_stage(python):
    compiler = os.environ.get("CC", "cc")
    if shutil.which(compiler) is None:
        return {"ok": True, "skip": True, "counts": "skipped (no %s on PATH)" % compiler,
                "seconds": 0.0, "tail": ""}
    code, output, seconds = run(["make", "-C", ROOT])
    built = os.path.exists(os.path.join(ROOT, "minidb",
                                        "_native%s" % _ext_suffix(python)))
    return {"ok": code == 0 and built, "skip": False,
            "counts": "native extension built" if built else "build failed",
            "seconds": seconds, "tail": output}


def _ext_suffix(python):
    code, output, _ = run([python, "-c",
                           "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))"])
    return output.strip() if code == 0 else ".so"


def tests_stage(python):
    code, output, seconds = run([python, "-m", "unittest", "discover", "-s", "tests"])
    ran = re.search(r"^Ran (\d+) tests?", output, re.M)
    failures = re.search(r"failures=(\d+)", output)
    errors = re.search(r"errors=(\d+)", output)
    count = ran.group(1) if ran else "?"
    detail = "%s tests, %s failures, %s errors" % (
        count, failures.group(1) if failures else "0", errors.group(1) if errors else "0")
    return {"ok": code == 0, "counts": detail, "seconds": seconds, "tail": output}


def corpus_stage(python):
    code, output, seconds = run([python, "tools/behavior_conformance.py"])
    match = re.search(r"BEHAVIOUR CONFORMANCE: (\d+)/(\d+)", output)
    files = len(set(re.findall(r"(tests/corpus/\S+): \d+/\d+", output)))
    count = "%s/%s cases across %d files" % (match.group(1), match.group(2), files) \
        if match else "no corpus score line"
    return {"ok": code == 0, "counts": count, "seconds": seconds, "tail": output}


def crash_stage(python, iterations):
    code, output, seconds = run([python, "tools/crash_torture.py",
                                 "--iterations", str(iterations), "--rows", "20"])
    match = re.search(r"CRASH TORTURE: (\d+)/(\d+)", output)
    count = "%s/%s iterations clean" % (match.group(1), match.group(2)) \
        if match else "no crash score line"
    return {"ok": code == 0, "counts": count, "seconds": seconds, "tail": output}


def docs_stage(python):
    code, output, seconds = run([python, "tools/docs_runner.py"])
    match = re.search(r"DOCS RUNNER: (\d+)/(\d+) blocks ran, (\d+) skipped", output)
    count = "%s/%s blocks ran, %s skipped" % match.groups() if match else "no docs score line"
    return {"ok": code == 0, "counts": count, "seconds": seconds, "tail": output}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100,
                        help="crash-sweep iterations (default 100; 300 was measured at 79s)")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--quiet", action="store_true", help="summary block only")
    args = parser.parse_args()

    python = args.python
    head, dirty = revision()
    started = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

    stages = [
        ("build", lambda: build_stage(python)),
        ("tests", lambda: tests_stage(python)),
        ("corpus", lambda: corpus_stage(python)),
        ("crash", lambda: crash_stage(python, args.iterations)),
        ("docs", lambda: docs_stage(python)),
    ]

    results = []
    for name, stage in stages:
        result = stage()
        results.append((name, result))
        if not args.quiet:
            status = "SKIP" if result.get("skip") else ("OK" if result["ok"] else "FAIL")
            print("[%s] %-7s %-52s %6.1fs" % (status, name, result["counts"][:52],
                                              result["seconds"]), flush=True)

    failed = [name for name, result in results if not result["ok"]]
    passed = sum(1 for _name, result in results if result["ok"])
    print()
    print("=" * 78)
    print("VERIFY  revision %s%s" % (head, " (+%d dirty files)" % dirty if dirty else ""))
    print("  started %s   python %s   loadavg %s" % (started, sys.version.split()[0], loadavg()))
    print("-" * 78)
    for name, result in results:
        status = "SKIP" if result.get("skip") else ("OK" if result["ok"] else "FAIL")
        print("  %-6s %-4s %-52s %6.1fs"
              % (name, status, result["counts"][:52], result["seconds"]))
    print("-" * 78)
    print("  RESULT: %s  (%d/%d stages)%s"
          % ("PASS" if not failed else "FAIL", passed, len(results),
             "" if not failed else "  failed: " + ", ".join(failed)))
    print("=" * 78)

    if failed and not args.quiet:
        for name, result in results:
            if result["ok"]:
                continue
            print()
            print("---- %s: raw failure output " % name + "-" * (48 - len(name)))
            lines = [line for line in result["tail"].splitlines() if line.strip()]
            # The score lines at the end of a harness's output are the summary; the mismatch that
            # caused the failure sits above them. Show the diagnostic lines first, then the tail,
            # so a failure is readable rather than merely present.
            diagnostic = [line for line in lines
                          if re.search(r"\bFAIL\b|\bERROR\b|DISCARD|Traceback|mismatch", line)]
            for line in diagnostic[:20]:
                print("  " + line)
            if diagnostic:
                print("  ...")
            for line in lines[-12:]:
                print("  " + line)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
