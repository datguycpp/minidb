"""Milestone 36 tests: replication to a second instance, policy REJECT.

The declared policy (docs/tier6-prework.md): a ship whose base does not match the replica is
refused with a typed error naming the base seq; nothing is merged, and a divergent replica's
local commits are reported before resolution discards them. Acceptance is invariants, and each
one here is checked the way it can fail: byte-identity after a ship, pre-or-post state after a
kill inside the apply, row comparison after a heal — never `integrity_check`, which passes a
file whose rows are simply gone.
"""

import hashlib
import os
import shutil
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402
from minidb.replication import (Primary, Replica, ReplicationConflict,  # noqa: E402
                                ReplicationError, sha256_file)
from minidb.storage.pager import PAGE_SIZE, VERSION, read_repl_log  # noqa: E402

SITES = ["wal_before_write", "wal_after_write", "wal_after_flush", "wal_after_fsync",
         "db_before_apply", "db_mid_apply", "db_after_first_page", "db_after_flush",
         "db_after_fsync", "truncate_before", "truncate_after", "db_torn_page"]


def remove_db(path):
    for suffix in ("", ".wal", ".replog", ".shipped", ".shipped.tmp"):
        target = path + suffix
        if os.path.exists(target):
            os.remove(target)


class ReplicationCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.primary_path = os.path.join(self.dir, "primary.db")
        self.replica_path = os.path.join(self.dir, "replica.db")

    def tearDown(self):
        for path in (self.primary_path, self.replica_path):
            remove_db(path)
        shutil.rmtree(self.dir, ignore_errors=True)

    def seed(self, rows=5):
        primary = Primary(self.primary_path)
        primary.run("CREATE TABLE t (id INTEGER, v TEXT, n REAL, b BLOB)")
        primary.run("CREATE INDEX idx_v ON t (v)")
        for i in range(1, rows + 1):
            primary.run(f"INSERT INTO t VALUES ({i}, 'x{i}', {i}.5, X'{i:02X}')")
        primary.run("INSERT INTO t VALUES (NULL, NULL, NULL, NULL)")
        return primary

    def rows(self, source, sql="SELECT id, v, n, typeof(b) FROM t ORDER BY id"):
        """Read through a live handle when one exists: the writer holds LOCK_EX for its lifetime."""
        if hasattr(source, "run"):
            return source.run(sql)[1]
        db = Database(source)
        try:
            return db.run(sql)[1]
        finally:
            db.close()


class TestConvergence(ReplicationCase):
    def test_ship_makes_the_replica_byte_identical_and_answers_agree(self):
        primary = self.seed()
        replica = Replica(self.replica_path)
        self.assertEqual(replica.lag(primary), primary.head())
        report = replica.sync(primary)
        self.assertEqual(report.applied, primary.head())
        self.assertEqual(replica.lag(primary), 0)
        self.assertEqual(sha256_file(self.replica_path), primary.sha())
        # Byte-identity is the strong form; answers are checked because that is what a user sees.
        self.assertEqual(self.rows(self.replica_path), self.rows(primary.db))
        self.assertEqual(self.rows(self.replica_path, "SELECT count(*) FROM t"),
                         self.rows(primary.db, "SELECT count(*) FROM t"))
        primary.close()

    def test_every_incremental_ship_keeps_the_identity(self):
        primary = self.seed(rows=2)
        replica = Replica(self.replica_path)
        for step in range(5):
            primary.run(f"INSERT INTO t VALUES ({100 + step}, 'step{step}', 0.0, X'00')")
            replica.sync(primary)
            self.assertEqual(sha256_file(self.replica_path), primary.sha(),
                             f"identity broken after ship {step}")
        # Shipping is not a local commit: the replica's own log must stay empty.
        self.assertEqual(replica.local_commits(), (0, None))
        primary.close()

    def test_replay_of_the_same_batch_is_idempotent(self):
        primary = self.seed(rows=3)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        primary.run("INSERT INTO t VALUES (77, 'replay', 1.0, X'77')")
        batch = primary.batches_since(replica.marker()[0])[-1]
        replica._apply(batch)
        once = sha256_file(self.replica_path)
        replica._apply(batch)
        self.assertEqual(sha256_file(self.replica_path), once)
        primary.close()


class TestSchemaAndShrink(ReplicationCase):
    def test_schema_changes_replicate_byte_identically(self):
        primary = self.seed(rows=2)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        primary.run("CREATE INDEX idx_n ON t (n)")
        replica.sync(primary)
        self.assertEqual(sha256_file(self.replica_path), primary.sha())
        primary.run("ALTER TABLE t ADD COLUMN extra TEXT")
        replica.sync(primary)
        self.assertEqual(sha256_file(self.replica_path), primary.sha())
        primary.run("CREATE TABLE other (k INTEGER PRIMARY KEY, payload BLOB)")
        primary.run("INSERT INTO other VALUES (1, X'DEADBEEF')")
        replica.sync(primary)
        self.assertEqual(sha256_file(self.replica_path), primary.sha())
        primary.close()

    def test_vacuum_that_shrinks_the_file_still_converges(self):
        primary = Primary(self.primary_path)
        primary.run("CREATE TABLE big (id INTEGER, body TEXT)")
        payload = "x" * 900
        for i in range(1, 21):
            primary.run(f"INSERT INTO big VALUES ({i}, '{payload}')")
        replica = Replica(self.replica_path)
        replica.sync(primary)
        size_before = os.path.getsize(self.primary_path)
        primary.run("DELETE FROM big WHERE id > 3")
        primary.run("VACUUM")
        size_after = os.path.getsize(self.primary_path)
        self.assertLess(size_after, size_before)
        replica.sync(primary)
        self.assertEqual(sha256_file(self.replica_path), primary.sha())
        self.assertEqual(os.path.getsize(self.replica_path), size_after)
        check = Database(self.replica_path)
        try:
            self.assertEqual(check.run("SELECT count(*) FROM big")[1], [(3,)])
        finally:
            check.close()
        primary.close()


    def test_crash_between_vacuum_rename_and_checkpoint_fails_loudly(self):
        """The declared residual: a VACUUM crash in the rename window leaves the log one
        checkpoint behind. The next ship must fail the identity check, never converge silently."""
        primary = Primary(self.primary_path)
        primary.run("CREATE TABLE big (id INTEGER, body TEXT)")
        payload = "y" * 900
        for i in range(1, 16):
            primary.run(f"INSERT INTO big VALUES ({i}, '{payload}')")
        replica = Replica(self.replica_path)
        replica.sync(primary)
        primary.close()

        pid = os.fork()
        if pid == 0:
            try:
                db = Database(self.primary_path, repl_log=True)
                os.environ["MINIDB_CRASH_POINT"] = "vacuum_after_replace"
                db.run("DELETE FROM big WHERE id > 3")
                db.run("VACUUM")
            except BaseException:
                os._exit(70)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        os.environ.pop("MINIDB_CRASH_POINT", None)
        self.assertEqual(os.waitstatus_to_exitcode(status), 9)

        primary = Primary(self.primary_path)
        with self.assertRaises(ReplicationError) as ctx:
            replica.sync(primary)
        self.assertNotIsInstance(ctx.exception, ReplicationConflict)
        self.assertIn("identity invariant broken", str(ctx.exception))
        resolution = replica.resolve(primary)
        self.assertGreaterEqual(resolution.discarded, 0)
        self.assertEqual(sha256_file(self.replica_path), primary.sha())
        primary.close()


class TestDivergence(ReplicationCase):
    def test_conflict_names_the_base_seq_and_counts_local_commits(self):
        primary = self.seed(rows=2)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        base = replica.marker()[0]

        handle = replica.open()
        handle.run("INSERT INTO t VALUES (90, 'local-a', 0.0, X'90')")
        handle.run("INSERT INTO t VALUES (91, 'local-b', 0.0, X'91')")
        handle.close()
        primary.run("INSERT INTO t VALUES (92, 'primary', 0.0, X'92')")

        before = (sha256_file(self.replica_path), primary.sha())
        with self.assertRaises(ReplicationConflict) as ctx:
            replica.sync(primary)
        error = ctx.exception
        self.assertEqual(error.base_seq, base)
        self.assertEqual(error.refused_seq, base + 1)
        self.assertEqual(error.local_commits, 2)
        self.assertEqual(error.local_seq_range, (1, 2))
        self.assertIn(f"base seq {base}", str(error))
        # The negative control: a refusal that mutated either file is not a policy outcome.
        self.assertEqual((sha256_file(self.replica_path), primary.sha()), before)
        primary.close()

    def test_divergence_is_refused_even_with_no_batch_to_ship(self):
        primary = self.seed(rows=2)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        handle = replica.open()
        handle.run("INSERT INTO t VALUES (88, 'local-only', 0.0, X'88')")
        handle.close()
        self.assertEqual(primary.head(), replica.marker()[0])      # nothing new to ship
        with self.assertRaises(ReplicationConflict) as ctx:
            replica.sync(primary)
        self.assertEqual(ctx.exception.local_commits, 1)
        self.assertIsNone(ctx.exception.refused_seq)
        self.assertIn("no batch to ship", str(ctx.exception))
        primary.close()

    def test_a_change_outside_the_replication_log_is_still_refused(self):
        primary = self.seed(rows=2)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        plain = Database(self.replica_path)          # not a replicated handle: unlogged
        plain.run("INSERT INTO t VALUES (93, 'rogue', 0.0, X'93')")
        plain.close()
        primary.run("INSERT INTO t VALUES (94, 'primary', 0.0, X'94')")
        with self.assertRaises(ReplicationConflict) as ctx:
            replica.sync(primary)
        self.assertEqual(ctx.exception.local_commits, 0)
        self.assertTrue(ctx.exception.file_changed)
        primary.close()


class TestHeal(ReplicationCase):
    def test_resolution_reports_the_discarded_commits_then_adopts_primary(self):
        primary = self.seed(rows=2)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        acknowledged = self.rows(primary.db)      # what the primary had committed

        handle = replica.open()
        handle.run("INSERT INTO t VALUES (90, 'local', 0.0, X'90')")
        handle.close()
        primary.run("INSERT INTO t VALUES (95, 'primary', 0.0, X'95')")

        with self.assertRaises(ReplicationConflict):
            replica.sync(primary)
        resolution = replica.resolve(primary)
        self.assertEqual(resolution.discarded, 1)
        self.assertEqual(resolution.seq_range, (1, 1))
        self.assertEqual(sha256_file(self.replica_path), primary.sha())
        self.assertEqual(replica.lag(primary), 0)

        healed = self.rows(self.replica_path)
        # No acknowledged row lost — row comparison, the way a lost row is actually visible.
        for row in acknowledged:
            self.assertIn(row, healed)
        self.assertNotIn((90, "local", 0.0, "blob"), healed)   # declared discard, reported above
        primary.close()

    def test_heal_sequence_refused_ship_then_resolution_then_resumed_shipping(self):
        primary = self.seed(rows=2)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        handle = replica.open()
        handle.run("INSERT INTO t VALUES (90, 'local', 0.0, X'90')")
        handle.close()
        primary.run("INSERT INTO t VALUES (97, 'unshipped', 0.0, X'97')")
        with self.assertRaises(ReplicationConflict):
            replica.sync(primary)
        replica.resolve(primary)
        primary.run("INSERT INTO t VALUES (96, 'after-heal', 0.0, X'96')")
        report = replica.sync(primary)
        self.assertEqual(report.applied, 1)
        self.assertEqual(sha256_file(self.replica_path), primary.sha())
        self.assertIn((96, "after-heal", 0.0, "blob"), self.rows(self.replica_path))
        primary.close()


class TestLag(ReplicationCase):
    def test_lag_is_a_readout_and_the_replica_answers_from_its_own_state(self):
        primary = self.seed(rows=2)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        before = self.rows(self.replica_path, "SELECT count(*) FROM t")
        for step in range(3):
            primary.run(f"INSERT INTO t VALUES ({200 + step}, 'late{step}', 0.0, X'00')")
        self.assertEqual(replica.lag(primary), 3)
        self.assertEqual(self.rows(self.replica_path, "SELECT count(*) FROM t"), before)
        replica.sync(primary)
        self.assertEqual(replica.lag(primary), 0)
        self.assertEqual(self.rows(self.replica_path, "SELECT count(*) FROM t"),
                         self.rows(primary.db, "SELECT count(*) FROM t"))
        primary.close()


class TestApplyPathCrash(ReplicationCase):
    def _child_apply(self, path, batch, site):
        pid = os.fork()
        if pid == 0:
            try:
                os.environ["MINIDB_CRASH_POINT"] = site
                os.environ["MINIDB_CRASH_COMMIT"] = "1"
                Replica(path)._apply(batch)
            except BaseException:
                os._exit(70)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        os.environ.pop("MINIDB_CRASH_POINT", None)
        os.environ.pop("MINIDB_CRASH_COMMIT", None)
        return os.waitstatus_to_exitcode(status)

    def test_kill_inside_apply_leaves_pre_or_post_state_never_a_mix(self):
        primary = self.seed(rows=3)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        primary.run("INSERT INTO t VALUES (300, 'in-flight', 0.0, X'00')")
        primary.run("INSERT INTO t VALUES (301, 'in-flight-2', 0.0, X'01')")
        batch = primary.batches_since(replica.marker()[0])[0]

        pre_sha = sha256_file(self.replica_path)
        shadow = os.path.join(self.dir, "shadow.db")
        shutil.copyfile(self.replica_path, shadow)
        Replica(shadow)._apply(batch)
        post_sha = sha256_file(shadow)
        self.assertNotEqual(pre_sha, post_sha)

        died, failures = 0, []
        for site in SITES:
            work = os.path.join(self.dir, f"work-{site}.db")
            shutil.copyfile(self.replica_path, work)
            code = self._child_apply(work, batch, site)
            if code == 9:
                died += 1
            elif code != 0:
                failures.append((site, f"child exit {code}"))
                continue
            try:
                Database(work).close()               # recovery runs on open, as in production
            except BaseException as exc:             # a torn file that will not open is a failure
                failures.append((site, f"reopen raised {exc!r}"))
                continue
            final = sha256_file(work)
            if final not in (pre_sha, post_sha):
                failures.append((site, f"state is neither pre nor post ({final[:12]})"))
            remove_db(work)
        self.assertEqual(failures, [])
        self.assertEqual(died, len(SITES), "every site must actually fire, or this is a lottery")
        primary.close()


class TestLogDiscipline(ReplicationCase):
    def test_recovery_replays_and_logs_a_batch_the_file_had_not_applied(self):
        """The crash window between the file fsync and the log append: the WAL is the safety net."""
        primary = self.seed(rows=2)
        head_before = primary.head()
        primary.close()                      # the child writer needs the exclusive lock
        pid = os.fork()
        if pid == 0:
            try:
                db = Database(self.primary_path, repl_log=True)
                os.environ["MINIDB_CRASH_POINT"] = "wal_after_fsync"
                os.environ["MINIDB_CRASH_COMMIT"] = "1"
                db.run("INSERT INTO t VALUES (400, 'recovered', 0.0, X'00')")
            except BaseException:
                os._exit(70)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        os.environ.pop("MINIDB_CRASH_POINT", None)
        os.environ.pop("MINIDB_CRASH_COMMIT", None)
        self.assertEqual(os.waitstatus_to_exitcode(status), 9)

        reopened = Database(self.primary_path, repl_log=True)   # recovery applies the WAL batch
        reopened.close()
        records = read_repl_log(self.primary_path + ".replog")
        self.assertEqual(len(records), head_before + 1)
        self.assertIn((400, "recovered", 0.0, "blob"), self.rows(self.primary_path))

    def test_recovery_does_not_log_the_same_batch_twice(self):
        """The other window: the log already has the batch and the WAL still replays it."""
        primary = self.seed(rows=2)
        head_before = primary.head()
        primary.close()                      # the child writer needs the exclusive lock
        pid = os.fork()
        if pid == 0:
            try:
                db = Database(self.primary_path, repl_log=True)
                os.environ["MINIDB_CRASH_POINT"] = "truncate_before"
                os.environ["MINIDB_CRASH_COMMIT"] = "1"
                db.run("INSERT INTO t VALUES (401, 'logged-once', 0.0, X'00')")
            except BaseException:
                os._exit(70)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        os.environ.pop("MINIDB_CRASH_POINT", None)
        os.environ.pop("MINIDB_CRASH_COMMIT", None)
        self.assertEqual(os.waitstatus_to_exitcode(status), 9)

        reopened = Database(self.primary_path, repl_log=True)   # replays; must not re-log
        reopened.close()
        records = read_repl_log(self.primary_path + ".replog")
        self.assertEqual(len(records), head_before + 1)
        self.assertIn((401, "logged-once", 0.0, "blob"), self.rows(self.primary_path))

    def test_format_version_stays_two(self):
        primary = self.seed(rows=1)
        replica = Replica(self.replica_path)
        replica.sync(primary)
        for path in (self.primary_path, self.replica_path):
            with open(path, "rb") as handle:
                header = handle.read(12)
            self.assertEqual(header[8], VERSION)
            self.assertEqual(VERSION, 2)
        primary.close()

    def test_the_log_is_readable_and_sequenced_without_gaps(self):
        primary = self.seed(rows=1)
        records = read_repl_log(primary.path + ".replog")
        self.assertEqual([seq for seq, _ in records], list(range(1, len(records) + 1)))
        self.assertTrue(all(page_id in pages for _, pages in records for page_id in pages))
        primary.close()


if __name__ == "__main__":
    unittest.main()
