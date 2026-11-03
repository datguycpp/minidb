"""Skeptic round 2, surface 1 — real SIGKILL during VACUUM (charter goal 5).

Pre-registered in LOG.md: 30 kills at stratified seeded delays in [0, D] plus
8 controls in (D, D+0.2s], D = vacuum duration from calibration. The existing
machinery kills at seeded crash POINTS via os._exit(9) — cooperative, between
operations. This probe sends an actual SIGKILL from the parent, which lands
where no crash point can be placed: mid-syscall.

Invariants (counts and thresholds unchanged from the pre-registration):
  K1  reopen succeeds or raises a typed (minidb-module) error — no hang (10 s)
  K2  PRAGMA integrity_check reads 'ok'
  K3  row comparison: exactly the survivors with the expected id-sum
  K4  post-kill insert + select round-trip works
  K5  an IN-WINDOW kill's victim really died of SIGKILL (returncode -9).
      Controls are past the window by design: they must complete (rc 0) or be
      caught by the kill (rc -9); a completed control whose file is unchanged
      is a violation — the blindness this fixture exists to prevent.

State classification: the killed file's sha must be the pre-vacuum or
post-vacuum sha (post from an identical completed copy). The fixture keeps its
freelist (no build-time vacuum) so the two states differ, and the instrument
ABORTS with RESULT: ERROR if calibration shows pre == post — it refuses to
report an unmeasurable window as evidence.

Round-1 defect notes (re-authored, disclosed): the v1 fixture was vacuumed at
build time, so every iteration's vacuum was a byte-identical no-op (states
pre=38 post=0, window unobservable), and K5 was misapplied to controls that
finished before their kill landed (5 false violations). Both fixed here;
thresholds and iteration counts are not.

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
    if not os.path.exists(path):
        return None              # same convention as replication.sha256_file
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
    """Built WITH its freelist and WITHOUT a build-time vacuum: the per-run
    vacuum must do real work so the pre and post states differ."""
    from minidb.engine import Database
    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.run("BEGIN")
    body = "x" * 80
    for i in range(1, ROWS + 1):
        db.run(f"INSERT INTO t VALUES ({i}, '{body}')")
    db.run("COMMIT")
    db.run(f"DELETE FROM t WHERE id <= {ROWS - KEEP}")
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


def run_iteration(work, delta, kind, label):
    """Returns (violations, probe_errors, record)."""
    violations, probe = [], []
    record = {"label": label, "kind": kind, "delta": round(delta, 4)}
    pre_sha = sha_of(work)

    # post state from an identical copy driven to completion
    post = work + ".post"
    shutil.copyfile(work, post)
    proc = spawn(["--child", post])
    line = read_line(proc, HANG)
    if line is None or line.strip() != "VACUUM-START":
        try:
            proc.kill()
            proc.wait(timeout=HANG)
        except Exception:  # noqa: BLE001
            pass
        err = (proc.stderr.read() or "").strip()
        probe.append(f"{label}: post-copy marker not seen ({line!r}) "
                     f"stderr={err[:400]!r}")
        remove_sidecars(post)
        return violations, probe, record
    proc.wait(timeout=HANG)
    post_sha = sha_of(post)
    remove_sidecars(post)
    if post_sha == pre_sha:
        probe.append(f"{label}: completed vacuum left the file byte-identical "
                     f"(pre==post) — window unmeasurable with this fixture")
        return violations, probe, record

    # the actual kill iteration on a fresh identical copy
    work2 = work + ".kill"
    shutil.copyfile(work, work2)
    proc = spawn(["--child", work2])
    line = read_line(proc, HANG)
    if line is None:
        try:
            proc.kill()
            proc.wait()
        except Exception:  # noqa: BLE001
            pass
        probe.append(f"{label}: no VACUUM-START within {HANG}s (hang)")
        record["outcome"] = "hang-before-marker"
        remove_sidecars(work2)
        return violations, probe, record
    if line.strip() != "VACUUM-START":
        try:
            proc.kill()
            proc.wait(timeout=HANG)
        except Exception:  # noqa: BLE001
            pass
        err = (proc.stderr.read() or "").strip()
        probe.append(f"{label}: marker not seen ({line!r}) stderr={err[:400]!r}")
        record["outcome"] = f"bad-marker:{line!r}"
        remove_sidecars(work2)
        return violations, probe, record

    time.sleep(delta)
    try:
        os.kill(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass                      # already exited; rc below reports which way
    rc = proc.wait(timeout=HANG)
    record["rc"] = rc

    if kind == "in":
        if rc != -signal.SIGKILL:
            err = (proc.stderr.read() or "").strip()
            violations.append(f"{label}: K5 victim exit {rc}, expected -9 "
                              f"(in-window kill missed the victim); "
                              f"stderr={err[:300]!r}")
    else:  # control: past the window by design
        if rc not in (0, -signal.SIGKILL):
            err = (proc.stderr.read() or "").strip()
            violations.append(f"{label}: control victim crashed with exit "
                              f"{rc}; stderr={err[:300]!r}")

    state_sha = sha_of(work2)
    if state_sha is None:
        state = "MISSING"
        violations.append(f"{label}: victim file ABSENT after kill "
                          f"(unlink-rename window) — not pre, not post")
    elif state_sha == pre_sha:
        state = "pre"
    elif state_sha == post_sha:
        state = "post"
    else:
        state = "OTHER"
        violations.append(f"{label}: state neither pre nor post "
                          f"({state_sha[:12]} pre={pre_sha[:12]} "
                          f"post={post_sha[:12]})")
    record["state"] = state

    if kind == "ctl" and rc == 0 and state != "post":
        violations.append(f"{label}: completed vacuum left state={state} — "
                          f"a finished vacuum must land post")

    # observability only: files left behind by the killed run (no verdict)
    base = os.path.basename(work2)
    strays = sorted(f for f in os.listdir(os.path.dirname(work2))
                    if f.startswith(base) and f not in (base, base + ".wal"))
    record["strays"] = strays

    if state == "MISSING":
        remove_sidecars(work2)
        return violations, probe, record      # K1-K4 need a file to open

    from minidb.engine import Database
    try:
        db = Database(work2)
    except Exception as exc:  # noqa: BLE001
        typed = exc.__class__.__module__.split(".")[0] == "minidb"
        if not typed:
            violations.append(f"{label}: reopen raised non-minidb {exc!r}")
        record["outcome"] = "reopen-" + ("typed" if typed else "UNEXPECTED")
        remove_sidecars(work2)
        return violations, probe, record
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
        record["outcome"] = "checked"
    finally:
        db.close()
    remove_sidecars(work2)
    return violations, probe, record


def calibrate(master, tmp):
    """Three no-kill runs; D = max. Returns the list, or None to abort."""
    Ds = []
    for c in range(3):
        calib = os.path.join(tmp, f"calib{c}.db")
        shutil.copyfile(master, calib)
        pre_c = sha_of(calib)
        proc = spawn(["--child", calib])
        line = read_line(proc, HANG)
        if not (line and line.strip() == "VACUUM-START"):
            try:
                proc.kill()
                proc.wait(timeout=HANG)
            except Exception:  # noqa: BLE001
                pass
            print(f"PROBE-ERROR: calibration {c} marker not seen "
                  f"({line!r}) stderr={(proc.stderr.read() or '')[:400]!r}")
            return None
        start = time.time()
        end = read_line(proc, HANG)
        proc.wait(timeout=HANG)
        if not (end and end.strip() == "VACUUM-END"):
            print(f"PROBE-ERROR: calibration {c} end marker not seen "
                  f"({end!r}) stderr={(proc.stderr.read() or '')[:400]!r}")
            return None
        Ds.append(time.time() - start)
        post_c = sha_of(calib)
        if post_c == pre_c:
            print("PROBE-ERROR: completed vacuum left the file byte-identical "
                  "(pre==post) — the window is unmeasurable with this fixture; "
                  "refusing to run blind")
            return None
        remove_sidecars(calib)
    return Ds


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
    print(f"kill: real SIGKILL from parent at stratified delays; "
          f"hang bound {HANG}s; fixture keeps its freelist (no build vacuum)")

    started = time.time()
    build_master(master)
    print(f"fixture built in {time.time() - started:.1f}s")
    print(f"wal after close: {os.path.exists(master + '.wal')}")

    Ds = calibrate(master, tmp)
    if Ds is None:
        return 2
    D = max(Ds)
    print("calibration: D = max of "
          + ", ".join(f"{d * 1000:.1f}" for d in Ds)
          + f" ms -> D = {D * 1000:.1f} ms; pre != post verified on all "
            f"{len(Ds)} runs")

    plan = []
    for i in range(N_INWINDOW):
        plan.append(("in", D * (i + rng.random()) / N_INWINDOW))
    for i in range(N_CONTROL):
        plan.append(("ctl", D + 0.02 + i * 0.02 + rng.random() * 0.01))

    violations, probe_errors = [], []
    states = {"pre": 0, "post": 0, "OTHER": 0, "MISSING": 0}
    missed = 0
    for idx, (kind, delta) in enumerate(plan):
        work = os.path.join(tmp, f"it{idx}.db")
        shutil.copyfile(master, work)
        try:
            vs, ps, rec = run_iteration(work, delta, kind, f"{kind}-{idx}")
        except Exception as exc:  # noqa: BLE001 - instrument defect, recorded
            probe_errors.append(f"{kind}-{idx}: probe raised {exc!r}")
            print(f"iter {idx:>2} {kind:<3} PROBE-ERROR {exc!r}")
            remove_sidecars(work)
            continue
        violations += vs
        probe_errors += ps
        state = rec.get("state", "-")
        states[state] = states.get(state, 0) + 1
        if kind == "in" and rec.get("rc") == 0:
            missed += 1
        print(f"iter {idx:>2} {kind:<3} delta={rec.get('delta'):<7} "
              f"rc={rec.get('rc')} state={state} "
              f"outcome={rec.get('outcome', '-')} "
              f"strays={rec.get('strays', [])} violations={len(vs)}")
        remove_sidecars(work)

    print(f"states: {states}  in-window missed (completed before kill): "
          f"{missed}/{N_INWINDOW}")
    print("window coverage: an in-window kill landing after the atomic replace "
          "shows post, before it shows pre; mid-replace would show OTHER or "
          "MISSING (violations)")
    print(f"iterations: {len(plan)} ({N_INWINDOW} in-window + {N_CONTROL} "
          f"controls), seed {SEED}")
    for item in probe_errors:
        print(f"  PROBE-ERROR {item}")
    if violations:
        print(f"VIOLATIONS: {len(violations)}")
        for item in violations:
            print(f"  {item}")
        print("RESULT: FAIL")
        return 1
    if probe_errors:
        print(f"VIOLATIONS: 0 but {len(probe_errors)} probe error(s) — "
              f"no engine verdict this run")
        print("RESULT: ERROR")
        return 2
    print("VIOLATIONS: 0")
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
