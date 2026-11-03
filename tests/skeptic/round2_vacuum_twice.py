"""FOLLOW-UP probe — VACUUM's staged artifacts (pre-registered in LOG after v2,
before this file existed).

v2's kill9 run listed `<db>.vacuum.wal` after every COMPLETED vacuum and
`<db>.vacuum` + its WAL after a killed one — listed before any reopen, so the
artifact policy was unknown. This settles:
  Q1  do the orphans survive a clean close and a subsequent open of the db?
  Q2  does a SECOND VACUUM on the same file still behave with the stale staged
      name and its WAL present — rows intact, integrity ok?

PASS (fixed in LOG before the run): both vacuums succeed, integrity_check is
'ok', and the row count/sum are preserved after each. File listings are printed
raw; whatever they show about artifacts is a triage observation, not a pass
condition. Exit: 0 pass, 1 data-invariant failure, 2 probe error.
"""
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402

ROWS = 3000
DELETE_UP_TO = 1500
EXPECTED_COUNT = ROWS - DELETE_UP_TO
EXPECTED_SUM = sum(range(DELETE_UP_TO + 1, ROWS + 1))


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


def main():
    print(f"command: {' '.join(sys.argv)}")
    print(f"rows: {ROWS} with id <= {DELETE_UP_TO} deleted (freelist kept); "
          f"expected survivors count={EXPECTED_COUNT} sum={EXPECTED_SUM}")
    tmp = tempfile.mkdtemp(prefix="sk2-vac2-")
    path = os.path.join(tmp, "v.db")
    violations = []

    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.run("BEGIN")
    body = "x" * 80
    for i in range(1, ROWS + 1):
        db.run(f"INSERT INTO t VALUES ({i}, '{body}')")
    db.run("COMMIT")
    db.run(f"DELETE FROM t WHERE id <= {DELETE_UP_TO}")
    db.close()
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
    print("RESULT: PASS  (listings above are the artifact observation for "
          "triage, not a pass condition)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
