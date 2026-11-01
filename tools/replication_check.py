#!/usr/bin/env python3
"""M36 replication evidence: identity, divergence, heal, lag, and the apply-path crash sweep.

The declared policy is reject (docs/tier6-prework.md). Nothing here is "the file opened and
looked fine": a successful ship is byte-identical by check, a refusal is byte-unchanged on both
sides by check, and a kill inside the apply is only accepted as the pre-state or the post-state.

The sweep follows M24's rule — a coverage column, not a pass count. `died_here` records whether
the child actually died at the site it was pointed at; a site that never fires prints its own
note and is counted as unreached rather than quietly counted clean.

    python3 tools/replication_check.py --iterations-per-site 20
"""

import argparse
import hashlib
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402
from minidb.replication import (Primary, Replica, ReplicationConflict,  # noqa: E402
                                sha256_file)

SITES = ["wal_before_write", "wal_after_write", "wal_after_flush", "wal_after_fsync",
         "db_before_apply", "db_mid_apply", "db_after_first_page", "db_after_flush",
         "db_after_fsync", "truncate_before", "truncate_after", "db_torn_page"]


def short(digest):
    return (digest or "none")[:12]


def rows(path, sql):
    db = Database(path)
    try:
        return db.run(sql)[1]
    finally:
        db.close()


def build(workdir):
    primary_path = os.path.join(workdir, "primary.db")
    replica_path = os.path.join(workdir, "replica.db")
    primary = Primary(primary_path)
    primary.run("CREATE TABLE t (id INTEGER, v TEXT, n REAL, b BLOB)")
    primary.run("CREATE INDEX idx_v ON t (v)")
    for i in range(1, 6):
        primary.run(f"INSERT INTO t VALUES ({i}, 'x{i}', {i}.5, X'0{i}')")
    return primary, replica_path


def probe(primary, replica_path):
    replica = Replica(replica_path)

    # 1. convergence without a partition
    report = replica.sync(primary)
    identical = sha256_file(replica_path) == primary.sha()
    print(f"  convergence        : {report.applied} batches shipped -> sha256 "
          f"{short(report.sha256)}; replica == primary: {identical}")

    # 2. incremental ships keep the identity
    ok = 0
    for step in range(6):
        primary.run(f"INSERT INTO t VALUES ({100 + step}, 'step{step}', 0.0, X'00')")
        replica.sync(primary)
        ok += sha256_file(replica_path) == primary.sha()
    print(f"  incremental ships  : {ok}/6 byte-identical after each ship")

    # 3. replay idempotence
    primary.run("INSERT INTO t VALUES (200, 'replay', 0.0, X'00')")
    batch = primary.batches_since(replica.marker()[0])[-1]
    replica._apply(batch)
    once = sha256_file(replica_path)
    replica._apply(batch)
    print(f"  replay idempotence : same batch twice -> sha unchanged: "
          f"{sha256_file(replica_path) == once}")

    # 4/5. divergence refused, both files untouched, local commits reported
    handle = replica.open()
    handle.run("INSERT INTO t VALUES (900, 'local-a', 0.0, X'90')")
    handle.run("INSERT INTO t VALUES (901, 'local-b', 0.0, X'91')")
    handle.close()
    acknowledged = primary.db.run("SELECT id FROM t ORDER BY id")[1]
    primary.run("INSERT INTO t VALUES (902, 'primary', 0.0, X'92')")
    before = (sha256_file(replica_path), primary.sha())
    try:
        replica.sync(primary)
        print("  divergence         : NOT REFUSED  <- failure")
        return 1
    except ReplicationConflict as exc:
        untouched = (sha256_file(replica_path), primary.sha()) == before
        print(f"  divergence         : {exc}")
        print(f"                       both files unchanged: {untouched}; "
              f"report: {exc.report()}")
        if not untouched:
            return 1

    # 5. heal: reported discard, then primary adopted, no acknowledged row lost
    resolution = replica.resolve(primary)
    healed = rows(replica_path, "SELECT id FROM t ORDER BY id")
    kept = sum(1 for (row_id,) in healed if row_id in {row_id for (row_id,) in acknowledged})
    same = sha256_file(replica_path) == primary.sha()
    print(f"  resolution         : {resolution}")
    print(f"  acknowledged rows  : {kept}/{len(acknowledged)} present after heal "
          f"(row comparison); replica == primary: {same}")

    # 6. lag is a readout, and the replica answers from its own committed state
    for step in range(3):
        primary.run(f"INSERT INTO t VALUES ({300 + step}, 'late{step}', 0.0, X'00')")
    lag = replica.lag(primary)
    own = rows(replica_path, "SELECT count(*) FROM t")[0][0]
    replica.sync(primary)
    print(f"  lag                : partitioned {lag} -> healed {replica.lag(primary)}; "
          f"replica answered from its own state ({own} rows) while behind")
    return 0



def sweep(primary, replica_path, iterations, workdir):
    """One row per site: how many children died there, and whether the file was pre or post."""
    replica = Replica(replica_path)
    primary.run("INSERT INTO t VALUES (500, 'in-flight', 1.0, X'05')")
    primary.run("INSERT INTO t VALUES (501, 'in-flight-2', 2.0, X'06')")
    batch = primary.batches_since(replica.marker()[0])[0]

    pre_sha = sha256_file(replica_path)
    shadow = os.path.join(workdir, "shadow.db")
    shutil.copyfile(replica_path, shadow)
    Replica(shadow)._apply(batch)
    post_sha = sha256_file(shadow)
    remove_db(shadow)

    print(f"APPLY-PATH CRASH SWEEP: iterations_per_site={iterations} "
          f"pre={short(pre_sha)} post={short(post_sha)}")
    print(f"  {'site':<20} {'iters':>6} {'died_here':>9} {'pre_or_post':>12} {'failures':>9}")
    failures_total = 0
    reached = 0
    died_total = 0
    for site in SITES:
        died = clean = 0
        failures = []
        for offset in range(iterations):
            work = os.path.join(workdir, f"{site}-{offset}.db")
            shutil.copyfile(replica_path, work)
            pid = os.fork()
            if pid == 0:
                try:
                    os.environ["MINIDB_CRASH_POINT"] = site
                    os.environ["MINIDB_CRASH_COMMIT"] = "1"
                    Replica(work)._apply(batch)
                except BaseException:
                    os._exit(70)
                os._exit(0)
            _, status = os.waitpid(pid, 0)
            os.environ.pop("MINIDB_CRASH_POINT", None)
            os.environ.pop("MINIDB_CRASH_COMMIT", None)
            code = os.waitstatus_to_exitcode(status)
            if code == 9:
                died += 1
            elif code != 0:
                failures.append(f"child exit {code}")
                continue
            try:
                Database(work).close()          # recovery runs on open, as in production
            except BaseException as exc:
                failures.append(f"reopen raised {exc!r}")
                continue
            state = sha256_file(work)
            if state in (pre_sha, post_sha):
                clean += 1
            else:
                failures.append(f"state neither pre nor post ({short(state)})")
            remove_db(work)
        reached += 1 if died else 0
        died_total += died
        failures_total += len(failures)
        note = "" if died else "   <- site never reached"
        print(f"  {site:<20} {iterations:>6} {died:>9} {clean:>12} {len(failures):>9}{note}")
        for failure in failures[:2]:
            print(f"      FAIL {failure}")
    print(f"COVERAGE: {reached}/{len(SITES)} sites reached, "
          f"{died_total} crashes injected, {failures_total} invariant failures")
    return 1 if failures_total else 0


def remove_db(path):
    for suffix in ("", ".wal", ".replog", ".shipped", ".shipped.tmp"):
        target = path + suffix
        if os.path.exists(target):
            os.remove(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations-per-site", type=int, default=20)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="m36-replication-")
    primary, replica_path = build(workdir)
    try:
        print("REPLICATION CHECK  (policy: reject, declared before testing)")
        failed = probe(primary, replica_path)
        failed += sweep(primary, replica_path, args.iterations_per_site, workdir)
        return 1 if failed else 0
    finally:
        primary.close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
