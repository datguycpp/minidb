"""Skeptic round 2, surface 1 — real SIGKILL during VACUUM (charter goal 5).

Pre-registered in LOG.md before this file existed: 30 kills at stratified seeded
delays in [0, D] (D = vacuum duration from a printed calibration run) plus 8
controls in (D, D+0.2s]. The existing machinery (make verify's sweep, M24, M36)
kills at seeded crash POINTS via os._exit(9) — cooperative, between operations.
This probe sends an actual SIGKILL from the parent, which lands where no crash
point can be placed: mid-syscall.

Invariants per iteration (boolean, no thresholds):
  K1  reopen succeeds or raises a typed (minidb-module) error — no hang (10 s)
  K2  PRAGMA integrity_check reads 'ok'
  K3  row comparison: exactly the survivors with the expected id-sum
      (never integrity_check alone — the round-1 lesson)
  K4  post-kill insert + select round-trip works
  K5  the victim really died of SIGKILL (returncode == -9)
Plus per-iteration state classification: sha of the killed file must be the
pre-vacuum or post-vacuum sha (post computed from an identical no-kill copy).

Usage:
  python3 tests/skeptic/round2_kill9.py             # full run
  python3 tests/skeptic/round2_kill9.py --child DB   # internal victim mode
"""
import hashlib
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

ROWS = 12000
KEEP = 6000
SEED = 20261103
N_INWINDOW = 30
N_CONTROL = 8
HANG = 10.0
EXPECTED_COUNT = KEEP
EXPECTED_SUM = sum(range(ROWS - KEEP + 1, ROWS + 1))


def sha_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_sidecars(path):
    for suffix in ("", ".wal"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def child(path):
    from minidb.engine import Database
    db = Database(path)
    print("VACUUM-START", flush=True)
    db.run("VACUUM")
    print("VACUUM-END", flush=True)
    db.close()


def build_master(path):
    from minidb.engine import Database
    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.run("BEGIN")
    body = "x" * 80
    for i in range(1, ROWS + 1):
        db.run(f"INSERT INTO t VALUES ({i}, '{body}')")
    db.run("COMMIT")
    db.run(f"DELETE FROM t WHERE id <= {ROWS - KEEP}")
    db.run("VACUUM")
    db.close()


def read_line(proc, timeout):
    """First stdout line within timeout; None = hang; '' = EOF."""
    box = []

    def target():
        try:
            box.append(proc.stdout.readline())
        except Exception as exc:  # noqa: BLE001 - recorded raw
            box.append(f"<reader error: {exc!r}>")

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    return box[0] if box else ""


def spawn(args):
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__)] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT)


def run_iteration(work, delta, label):
    """Kill work at delta after the VACUUM-START marker; check K1-K5."""
    violations = []
    record = {"label": label, "delta": round(delta, 4)}
    pre_sha = sha_of(work)
    # post state from an identical no-kill copy of the same bytes
    post = work + ".post"
    shutil.copyfile(work, post)
    proc = spawn(["--child", post])
    line = read_line(proc, HANG)
    if line is None or line.strip() != "VACUUM-START":
        err = ""
        try:
            proc.kill()
            proc.wait(timeout=HANG)
            err = (proc.stderr.read() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        violations.append(f"{label}: calibration-copy marker not seen "
                          f"({line!r}) stderr={err[:400]!r}")
        remove_sidecars(post)
        return violations, record
    proc.wait(timeout=HANG)                       # let the post copy finish
    post_sha = sha_of(post)
    remove_sidecars(post)

    # the actual kill iteration on a fresh identical copy
    work2 = work + ".kill"
    shutil.copyfile(work, work2)
    proc = spawn(["--child", work2])
    line = read_line(proc, HANG)
    if line is None:
        proc.kill()
        proc.wait()
        violations.append(f"{label}: no VACUUM-START within {HANG}s (hang)")
        record["outcome"] = "hang-before-marker"
        remove_sidecars(work2)
        return violations, record
    if line.strip() != "VACUUM-START":
        try:
            proc.kill()
            proc.wait(timeout=HANG)
        except Exception:  # noqa: BLE001
            pass
        err = (proc.stderr.read() or "").strip()
        violations.append(f"{label}: marker not seen ({line!r}) stderr={err[:400]!r}")
        record["outcome"] = f"bad-marker:{line!r}"
        remove_sidecars(work2)
        return violations, record
    time.sleep(delta)
    os.kill(proc.pid, signal.SIGKILL)
    rc = proc.wait(timeout=HANG)
    record["rc"] = rc
    if rc != -signal.SIGKILL:
        err = (proc.stderr.read() or "").strip()
        violations.append(f"{label}: victim exit {rc}, expected -9; stderr={err[:300]!r}")

    killed_sha = sha_of(work2)
    record["state"] = ("pre" if killed_sha == pre_sha
                       else "post" if killed_sha == post_sha else "OTHER")
    if record["state"] == "OTHER":
        violations.append(f"{label}: state neither pre nor post "
                          f"({killed_sha[:12]} pre={pre_sha[:12]} post={post_sha[:12]})")

    # K1 reopen (typed = minidb-module error, recorded; hang impossible here)
    from minidb.engine import Database
    try:
        db = Database(work2)
    except Exception as exc:  # noqa: BLE001
        typed = exc.__class__.__module__.split(".")[0] == "minidb"
        tag = "typed" if typed else "UNEXPECTED"
        if not typed:
            violations.append(f"{label}: reopen raised non-minidb {exc!r}")
        record["outcome"] = f"reopen-{tag}"
        remove_sidecars(work2)
        return violations, record
    try:
        got = db.run("PRAGMA integrity_check")[1][0][0]
        if got != "ok":
            violations.append(f"{label}: integrity_check -> {got!r}")
        count, idsum = db.run("SELECT count(*), sum(id) FROM t")[1][0]
        if count != EXPECTED_COUNT or idsum != EXPECTED_SUM:
            violations.append(f"{label}: rows (count={count}, sum={idsum}) != "
                              f"({EXPECTED_COUNT}, {EXPECTED_SUM})")
        try:
            db.run("INSERT INTO t VALUES (999999, 'post-kill')")
            back = db.run("SELECT v FROM t WHERE id = 999999")[1]
            if back != [("post-kill",)]:
                violations.append(f"{label}: round-trip read back {back!r}")
        except Exception as exc:  # noqa: BLE001
            violations.append(f"{label}: post-kill round trip raised {exc!r}")
        record["outcome"] = record.get("outcome", "") + "+checked"
    finally:
        db.close()
    remove_sidecars(work2)
    return violations, record


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
        return 0

    rng = random.Random(SEED)
    tmp = tempfile.mkdtemp(prefix="sk2-kill9-")
    master = os.path.join(tmp, "master.db")
    print(f"command: {' '.join(sys.argv)}")
    print(f"seed: {SEED}  rows: {ROWS}  keep: {KEEP}  "
          f"expected: count={EXPECTED_COUNT} sum={EXPECTED_SUM}")
    print(f"kill: real SIGKILL from parent at stratified delays; hang bound {HANG}s")

    started = time.time()
    build_master(master)
    print(f"fixture built in {time.time() - started:.1f}s")
    print(f"wal after close: {os.path.exists(master + '.wal')} "
          f"(copies take the .db only; a residual WAL would surface as a "
          f"count mismatch on iteration 0)")

    # calibration: vacuum duration D on a fresh copy, no kill
    calib = os.path.join(tmp, "calib.db")
    shutil.copyfile(master, calib)
    proc = spawn(["--child", calib])
    line = read_line(proc, HANG)
    D = None
    if line and line.strip() == "VACUUM-START":
        start = time.time()
        end = read_line(proc, HANG)
        if end and end.strip() == "VACUUM-END":
            D = time.time() - start
    proc.wait()
    if D is None:
        print("CALIBRATION FAILED; stderr:")
        print(proc.stderr.read())
        return 2
    print(f"calibration: vacuum duration D = {D * 1000:.1f}ms (no-kill control)")
    remove_sidecars(calib)

    plan = []
    for i in range(N_INWINDOW):
        plan.append(("in", D * (i + rng.random()) / N_INWINDOW))
    for i in range(N_CONTROL):
        plan.append(("ctl", D + 0.02 + i * 0.02 + rng.random() * 0.01))

    violations = []
    states = {"pre": 0, "post": 0, "OTHER": 0}
    for idx, (kind, delta) in enumerate(plan):
        work = os.path.join(tmp, f"it{idx}.db")
        shutil.copyfile(master, work)
        vs, rec = run_iteration(work, delta, f"{kind}-{idx}")
        violations += vs
        states[rec.get("state", "-")] = states.get(rec.get("state", "-"), 0) + 1
        print(f"iter {idx:>2} {kind:<3} delta={rec.get('delta'):<7} "
              f"rc={rec.get('rc')} state={rec.get('state', '-')} "
              f"outcome={rec.get('outcome', '-')} violations={len(vs)}")
        remove_sidecars(work)

    print(f"states: pre={states.get('pre', 0)} post={states.get('post', 0)} "
          f"other={states.get('OTHER', 0)} (window coverage: kills landing "
          f"inside the vacuum show pre, after it shows post)")
    print(f"iterations: {len(plan)} ({N_INWINDOW} in-window + {N_CONTROL} controls), "
          f"seed {SEED}")
    if violations:
        print(f"VIOLATIONS: {len(violations)}")
        for item in violations:
            print(f"  {item}")
        print("RESULT: FAIL")
        return 1
    print("VIOLATIONS: 0")
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
