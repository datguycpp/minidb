"""Run the frozen gate-3 baseline instrument against the frozen revision's own engine.

`baseline_g6.py` at `d89f689` hardcodes `sys.path.insert(0, "/home/grub/projects/minidb")`, so
executing it from a worktree imports **HEAD's** engine and silently produces a mixed-subject
baseline. This runner makes the frozen-revision run reproducible by construction:

1. it places the committed tiny fixture at `/tmp/g6-tiny.db` (the only path the frozen script reads)
   after checking its `(bytes, sha256)` against the pin;
2. it imports the worktree's package **before** the script runs, so the script's import resolves to
   the already-loaded module rather than the hardcoded path;
3. it asserts every `minidb` module it loaded lives under the worktree, and prints the engine path
   and the worktree revision into the artifact, so a mixed-subject run fails loudly instead of
   reporting a number.

Usage: python tests/skeptic/run_frozen_baseline.py [--worktree /tmp/minidb-d89f689]
"""

import argparse
import hashlib
import os
import runpy
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(REPO, "tests", "skeptic", "fixtures", "g6-tiny.db")
TINY_TUPLE = (12288, "64a9d5417b7ec44389ffbde5b7e6da52ec53aff157156fbfd3e3caed3a4d4c30")
LEGACY_TINY = "/tmp/g6-tiny.db"


def identity(path):
    with open(path, "rb") as handle:
        data = handle.read()
    return len(data), hashlib.sha256(data).hexdigest()


def deliver_fixture():
    if not os.path.exists(FIXTURE):
        raise SystemExit("frozen-runner: no committed fixture at %s" % FIXTURE)
    if identity(FIXTURE) != TINY_TUPLE:
        raise SystemExit("frozen-runner: committed fixture is not the pinned one: %s"
                         % (identity(FIXTURE),))
    if os.path.abspath(FIXTURE) != os.path.abspath(LEGACY_TINY):
        shutil.copyfile(FIXTURE, LEGACY_TINY)
    if identity(LEGACY_TINY) != TINY_TUPLE:
        raise SystemExit("frozen-runner: %s is not the pinned fixture after delivery" % LEGACY_TINY)
    return TINY_TUPLE


def load_worktree(worktree):
    if not os.path.isdir(os.path.join(worktree, "minidb")):
        raise SystemExit("frozen-runner: %s has no minidb package" % worktree)
    sys.path.insert(0, worktree)
    import minidb
    import minidb.engine
    import minidb.storage
    import minidb.storage.btree
    import minidb.storage.pager
    import minidb.storage.record

    loaded = [minidb, minidb.engine, minidb.storage, minidb.storage.btree,
              minidb.storage.pager, minidb.storage.record]
    # `minidb.storage` is a namespace package (no __init__.py), so its location is `__path__`.
    locations = []
    for module in loaded:
        if getattr(module, "__file__", None):
            locations.append(module.__file__)
        else:
            locations.extend(getattr(module, "__path__", []))
    outside = [path for path in locations if not os.path.abspath(path).startswith(worktree)]
    if outside:
        raise SystemExit("frozen-runner: mixed subject — these modules are not from the worktree: %s"
                         % outside)
    return minidb.engine.__file__


def revision(worktree):
    try:
        return subprocess.run(["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0] or "")
    parser.add_argument("--worktree", default=os.environ.get("G6_WORKTREE", "/tmp/minidb-d89f689"))
    args = parser.parse_args(argv)
    worktree = os.path.abspath(args.worktree)

    print("frozen-runner: worktree=%s revision=%s" % (worktree, revision(worktree)), flush=True)
    print("frozen-runner: fixture=%s %s" % (LEGACY_TINY, deliver_fixture()), flush=True)
    print("frozen-runner: engine=%s" % load_worktree(worktree), flush=True)

    script = os.path.join(worktree, "tests", "skeptic", "baseline_g6.py")
    if not os.path.exists(script):
        raise SystemExit("frozen-runner: %s has no baseline_g6.py" % worktree)
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
