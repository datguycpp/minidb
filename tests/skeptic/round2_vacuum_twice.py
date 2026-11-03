"""FOLLOW-UP probe — VACUUM's staged artifacts (pre-registered in LOG after v2,
before this file existed; Q3 added pre-run after the v4 triage, LOG records the
assertion clarification).

v2's kill9 run listed `<db>.vacuum.wal` after every COMPLETED vacuum and
`<db>.vacuum` + its WAL after a killed one — listed before any reopen, so the
artifact policy was unknown. This settles:
  Q1  do the orphans survive a clean close and a subsequent open of the db?
  Q2  does a SECOND VACUUM on the same file still behave with the stale staged
      name and its WAL present — rows intact, integrity ok?
  Q3  a SIGKILLed vacuum: orphans exist at the kill moment (a SIGKILL defeats
      completion cleanup by nature), and the next open must sweep them.

PASS (fixed in LOG before the run): both vacuums succeed, integrity_check is
'ok', the row count/sum are preserved after each, and after the killed-vacuum
reopen no `<db>.vacuum*` artifact remains anywhere in the directory with rows
intact. The after-kill listing itself is a raw observation, not an assertion —
a SIGKILL cannot be cleaned up by the code it interrupted. Exit: 0 pass,
1 data/artifact-invariant failure, 2 probe error.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402

ROWS = 3000
DELETE_UP_TO = 1500
EXPECTED_COUNT = ROWS - DELETE_UP_TO
EXPECTED_SUM = sum(range(DELETE_UP_TO + 1, ROWS + 1))
HANG = 10.0


def listing(tmp):
    return sorted(os.listdir(tmp))


def check(label, path, violations):
    db = Database(path)
    try:
        got = db.run("PRAGMA integrity_check")[1][0][0]
        count, idsum = db.run("SELECT count(*), sum(id) FROM t")[1][0]
    finally:
        db.close()
    print(f"{label}: integrity={got!r} count={count} sum={idsum} "
          f"(expected {EXPECTED_COUNT}, {EXPECTED_SUM})")
    if got != "ok":
        violations.append(f"{label}: integrity_check -> {got!r}")
    if count != EXPECTED_COUNT or idsum != EXPECTED_SUM:
        violations.append(f"{label}: rows ({count}, {idsum}) != "
                          f"({EXPECTED_COUNT}, {EXPECTED_SUM})")


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


def child(path):
    db = Database(path)
    print("VACUUM-START", flush=True)
    db.run("VACUUM")
    print("VACUUM-END", flush=True)
    db.close()


def build(path, body):
    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.run("BEGIN")
    for i in range(1, ROWS + 1):
        db.run(f"INSERT INTO t VALUES ({i}, '{body}')")
    db.run("COMMIT")
    db.run(f"DELETE FROM t WHERE id <= {DELETE_UP_TO}")
    db.close()


def remove_sidecars(path):
    for suffix in ("", ".wal"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
        return 0

    print(f"command: {' '.join(sys.argv)}")
    print(f"rows: {ROWS} with id <= {DELETE_UP_TO} deleted (freelist kept); "
          f"expected survivors count={EXPECTED_COUNT} sum={EXPECTED_SUM}")
    tmp = tempfile.mkdtemp(prefix="sk2-vac2-")
    path = os.path.join(tmp, "v.db")
    violations = []
    body = "x" * 80

    build(path, body)
    print(f"after build:  {listing(tmp)}")

    try:
        db = Database(path)
        db.run("VACUUM")
        db.close()
        print(f"after vac1:   {listing(tmp)}")
        check("vac1", path, violations)

        # Q1: does a plain reopen clean the artifacts?
        db = Database(path)
        db.close()
        print(f"after reopen: {listing(tmp)}")

        # Q2: a second vacuum on the same file, stale staged name present
        db = Database(path)
        db.run("VACUUM")
        db.close()
        print(f"after vac2:   {listing(tmp)}")
        check("vac2", path, violations)

        # Q3: SIGKILLed vacuum — orphans at the kill moment (raw observation),
        # assertion on the reopen state (the fix's sweep-on-open contract).
        kpath = os.path.join(tmp, "k.db")
        build(kpath, body)
        cal = os.path.join(tmp, "kcal.db")
        shutil.copyfile(kpath, cal)
        proc = spawn(["--child", cal])
        line = read_line(proc, HANG)
        Dk = None
        if line and line.strip() == "VACUUM-START":
            start = time.time()
            end = read_line(proc, HANG)
            if end and end.strip() == "VACUUM-END":
                Dk = time.time() - start
        proc.wait(timeout=HANG)
        remove_sidecars(cal)
        if Dk is None:
            raise RuntimeError(f"killed-phase calibration failed "
                               f"({line!r}); stderr="
                               f"{(proc.stderr.read() or '')[:300]!r}")

        proc = spawn(["--child", kpath])
        line = read_line(proc, HANG)
        if not (line and line.strip() == "VACUUM-START"):
            try:
                proc.kill()
                proc.wait(timeout=HANG)
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"killed-phase marker not seen ({line!r}) "
                               f"stderr={(proc.stderr.read() or '')[:300]!r}")
        time.sleep(Dk * 0.5)
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        rc = proc.wait(timeout=HANG)
        print(f"after killed vacuum (rc={rc}, Dk={Dk * 1000:.1f}ms, "
              f"kill at 50%): {listing(tmp)}")

        # the assertion: the next open sweeps the killed vacuum's orphans
        db = Database(kpath)
        try:
            kcount = db.run("SELECT count(*) FROM t")[1][0][0]
        finally:
            db.close()
        print(f"after killed-vacuum reopen: {listing(tmp)} rows: {[(kcount,)]}")
        strays = sorted(f for f in listing(tmp) if ".vacuum" in f)
        if strays:
            violations.append(f"vacuum artifacts remain after reopen: {strays}")
        if kcount != EXPECTED_COUNT:
            violations.append(f"after reopen: count {kcount} != "
                              f"{EXPECTED_COUNT}")
    except Exception as exc:  # noqa: BLE001
        violations.append(f"vacuum raised {exc!r}")
        traceback.print_exc()

    shutil.rmtree(tmp, ignore_errors=True)
    for item in violations:
        print(f"  VIOLATION {item}")
    print(f"violations: {len(violations)}")
    if violations:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS  (the after-kill listing above is the artifact "
          "observation for triage; assertions are on the completed and "
          "reopened states)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
