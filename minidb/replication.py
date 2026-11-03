"""Replication to a second instance (milestone 36), policy: REJECT.

There is no reference to conform to here, so acceptance is invariants (the M25 class). The
declared divergence policy is *reject at batch granularity*: a ship whose base does not match the
replica is refused with a typed error, and nothing is merged. That choice is not a preference —
page images are one side's world snapshot, so "applying the other side's batch" is overwrite by
definition, and last-writer-wins over page images deletes whole transactions' rows while leaving
a structurally valid file (`integrity_check` cannot see a row that is simply absent).

Transport is the committed batch itself: the primary's pager appends every commit to `<db>.replog`
with a sequence number, and the replica applies those pages through its own pager, so the replica
inherits the WAL's all-or-nothing apply and M24's crash sites rather than needing new ones. After
a successful ship the two files are byte-identical — the identity invariant is checked, not
assumed.

Heal semantics: the primary is authoritative. A divergent replica's local commits are counted and
reported (seq range) *before* resolution discards them, because losing a declared-divergent write
is allowed by policy and losing it silently is not (M27's rule).

Declared limits, stated before they are discovered:

- the change log is append-only and never compacted or checkpointed; a long-lived primary grows
  it without bound (the M19 freelist class of follow-up, not a hidden behaviour);
- shipping needs the replica quiescent: the apply takes the same exclusive lock a writer takes,
  so a replica with an open write handle refuses the ship with `database is locked`;
- resolution rebuilds the replica from the primary's file — it never merges, because a merge is
  exactly what this policy forbids;
- a commit whose pages are byte-identical to its predecessor is not logged (it changed no state,
  so the replica's bytes already agree);
- VACUUM rebuilds the file, so it ships as one full-image checkpoint batch. A crash between the
  rename and that append leaves the log one checkpoint behind: the next ship fails the identity
  check rather than converging silently, and resolve() rebuilds the replica.
"""

import json
import os
import shutil
import struct

from .engine import Database
from .storage.pager import (PAGE_SIZE, StorageError, _OFF_CATALOG, _OFF_PAGE_COUNT,
                            _OFF_VERSION, Pager, read_repl_log)


class ReplicationError(Exception):
    """A replication failure that is not a policy outcome (gap in the log, broken identity)."""


class ReplicationConflict(ReplicationError):
    """The replica's state is not the batch's base: the ship is refused, nothing is written.

    `local_commits` is what the replica wrote on its own since the last successful ship; the
    range is that log's sequence numbers. `file_changed` catches a writer that bypassed the
    replicated handle (nothing is invented to make the count look complete).
    """

    def __init__(self, base_seq, expected_seq, local_commits, local_seq_range, file_changed):
        self.base_seq = base_seq
        self.refused_seq = expected_seq
        self.local_commits = local_commits
        self.local_seq_range = local_seq_range
        self.file_changed = file_changed
        detail = f"{local_commits} local commit{'s' if local_commits != 1 else ''}"
        if local_seq_range:
            detail += f" (seq {local_seq_range[0]}..{local_seq_range[1]})"
        if file_changed and not local_commits:
            detail += " (file changed outside the replication log)"
        outcome = (f"batch {expected_seq} refused" if expected_seq is not None
                   else "no batch to ship, resolve required")
        super().__init__(f"replication conflict: replica diverged from base seq {base_seq} — "
                         f"{detail}; {outcome}")

    def report(self):
        return {"base_seq": self.base_seq, "refused_seq": self.refused_seq,
                "local_commits": self.local_commits, "local_seq_range": self.local_seq_range,
                "file_changed": self.file_changed}


class SyncReport:
    def __init__(self, applied, from_seq, to_seq, sha256, lag):
        self.applied = applied
        self.from_seq = from_seq
        self.to_seq = to_seq
        self.sha256 = sha256
        self.lag = lag

    def __repr__(self):
        return (f"SyncReport(applied={self.applied}, seq={self.from_seq}..{self.to_seq}, "
                f"lag={self.lag})")


class Resolution:
    """What resolution discarded, reported before it happened — never a silent rebuild."""

    def __init__(self, discarded, seq_range, adopted_seq, sha256):
        self.discarded = discarded
        self.seq_range = seq_range
        self.adopted_seq = adopted_seq
        self.sha256 = sha256

    def __repr__(self):
        return (f"Resolution(discarded={self.discarded}, seq_range={self.seq_range}, "
                f"adopted_seq={self.adopted_seq})")


def sha256_file(path):
    if not os.path.exists(path):
        return None
    import hashlib
    with open(path, "rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Primary:
    """A writable database whose commits are logged for shipping (`<db>.replog`)."""

    def __init__(self, path):
        self.path = path
        self.log_path = path + ".replog"
        self.db = Database(path, repl_log=True)

    def run(self, sql):
        return self.db.run(sql)

    def head(self):
        records = read_repl_log(self.log_path)
        return records[-1][0] if records else 0

    def sha(self):
        return sha256_file(self.path)

    def batches_since(self, seq):
        return [record for record in read_repl_log(self.log_path) if record[0] > seq]

    def close(self):
        self.db.close()


class Replica:
    """A second instance. Ships forward only; divergence is refused, never merged."""

    def __init__(self, path):
        self.path = path
        self.marker_path = path + ".shipped"

    # -- local writes while partitioned ------------------------------------
    def open(self):
        """A handle whose commits count as local: they are what a heal must report."""
        return Database(self.path, repl_log=True)

    def local_commits(self):
        records = read_repl_log(self.path + ".replog")
        if not records:
            return 0, None
        return len(records), (records[0][0], records[-1][0])

    # -- state --------------------------------------------------------------
    def marker(self):
        if not os.path.exists(self.marker_path):
            return 0, None
        with open(self.marker_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return int(data["seq"]), data.get("sha256")

    def lag(self, primary):
        """Batches the replica has not seen. A readout, not an inference from timings."""
        return max(0, primary.head() - self.marker()[0])

    # -- shipping ------------------------------------------------------------
    def sync(self, primary):
        seq, shipped_sha = self.marker()
        current = sha256_file(self.path)
        local, local_range = self.local_commits()
        pending = primary.batches_since(seq)
        # Divergence is refused even when there is nothing to ship: a replica that drifted and
        # reports lag 0 because the primary has not committed since is the silent shape this
        # policy exists to prevent.
        if current != shipped_sha or local:
            raise ReplicationConflict(seq, pending[0][0] if pending else None, local, local_range,
                                      current != shipped_sha)
        if not pending:
            # Nothing to ship is not the same as nothing to check. The marker above is written by
            # _apply, so a run that applied a batch and then failed its identity check leaves the
            # marker agreeing with the shipped sha while the replica has drifted: comparing the
            # replica to its own last write then reports lag 0 forever (skeptic round 2, R2b --
            # the comment above states this exact policy, and this path was the one that skipped
            # it). Identity is therefore verified against the primary on every sync, including
            # this one.
            primary_sha = primary.sha()
            if current != primary_sha:
                raise ReplicationConflict(seq, None, local, local_range, True)
            return SyncReport(0, seq, seq, current, self.lag(primary))

        from_seq = seq
        for expected, batch in enumerate(pending, start=seq + 1):
            if batch[0] != expected:
                raise ReplicationError(
                    f"replication log gap: expected batch {expected}, found {batch[0]}")
            self._apply(batch)

        replica_sha, primary_sha = sha256_file(self.path), primary.sha()
        if replica_sha != primary_sha:
            raise ReplicationError(
                f"identity invariant broken: replica {replica_sha} != primary {primary_sha}")
        to_seq = pending[-1][0]
        return SyncReport(len(pending), from_seq, to_seq, replica_sha, self.lag(primary))

    def resolve(self, primary):
        """Primary authoritative: report the divergent commits, then rebuild from the primary.

        The replica is replaced wholesale rather than patched — a patch is a merge, and a merge is
        the policy this milestone refused.
        """
        discarded, seq_range = self.local_commits()
        adopted = primary.head()
        for suffix in ("", ".wal", ".replog", ".shipped", ".rebuild"):
            target = self.path + suffix
            if os.path.exists(target):
                os.remove(target)
        # Staged and renamed, like VACUUM's replace: a crash mid-rebuild must not leave a
        # half-copied replica pretending to be one.
        staging = self.path + ".rebuild"
        shutil.copyfile(primary.path, staging)
        os.replace(staging, self.path)
        digest = self._write_marker(adopted)
        return Resolution(discarded, seq_range, adopted, digest)

    # -- internals -----------------------------------------------------------
    def _apply(self, batch):
        seq, pages = batch
        if 0 not in pages:
            raise ReplicationError(f"batch {seq} has no header page")
        header = pages[0]
        if len(header) != PAGE_SIZE:
            raise ReplicationError(f"batch {seq} header page is {len(header)} bytes")
        pager = Pager(self.path, repl_log=False)
        try:
            # The batch's own header is authoritative for the file's shape: the pager rebuilds
            # page 0 from these fields, which reproduces the primary's header byte for byte.
            pager.version = header[_OFF_VERSION]
            pager.page_count = struct.unpack_from("<I", header, _OFF_PAGE_COUNT)[0]
            pager.catalog_root = struct.unpack_from("<I", header, _OFF_CATALOG)[0]
            pager.dirty.update(pages)
            pager.commit()
        finally:
            pager.close()
        # A batch can shrink the file (VACUUM replaces the database with a shorter one); the
        # replica's length comes from the batch, never from whatever it had before.
        os.truncate(self.path, pager.page_count * PAGE_SIZE)
        self._write_marker(seq)

    def _write_marker(self, seq):
        digest = sha256_file(self.path)
        temp = self.marker_path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"seq": seq, "sha256": digest}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.marker_path)
        return digest
