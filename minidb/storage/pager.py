"""Fixed-size page file with a write-ahead log (milestone 7).

    page 0     header: magic "MiniDB01" | version | page_size | page_count | catalog_root
    page 1..n  B+tree nodes and catalog rows, addressed by page number

Durability model: statement writes land in a dirty-page buffer, never straight to the file. A
commit appends those pages to `<db>.wal`, fsyncs it, then applies them to the database file,
fsyncs again, and truncates the log. A crash therefore leaves either a log with a complete
commit marker — redo restores that commit — or a log tail without one, which is discarded and
the database is back to its last committed state.

    WAL frame    uint32 page_id | 4096-byte page | uint32 crc32(page)
    WAL commit   uint32 0xFFFFFFFF | uint32 frame_count | uint32 crc32(that batch's frames)

This is a redo log, not SQLite's WAL: one writer, no concurrent readers during recovery, no
incremental checkpointing. Those are M25 and M18.

Optional replication log (milestone 36, opt-in): with `repl_log=True` every committed batch is
also appended to `<db>.replog` with a sequence number, and recovery re-logs any batch it replays.
The file format is untouched — the log is a sidecar — which is what keeps M36 a milestone rather
than a format version. Identical consecutive batches are skipped: a commit whose pages match its
predecessor byte for byte changed no state, so the replica's bytes already agree.
"""

import fcntl
import os
import struct
import time
import zlib
from collections import OrderedDict

MAGIC = b"MiniDB01"
VERSION = 2                     # 2 adds a CRC32 trailer to every page
LEGACY_VERSION = 1              # read-only support: no checksums to verify
PAGE_SIZE = 4096
CHECKSUM_SIZE = 4
USABLE = PAGE_SIZE - CHECKSUM_SIZE

_OFF_MAGIC = 0
_OFF_VERSION = 8
_OFF_PAGE_SIZE = 9
_OFF_PAGE_COUNT = 11
_OFF_CATALOG = 15
HEADER_SIZE = 19

WAL_MAGIC = b"MWAL0001"
WAL_COMMIT = 0xFFFFFFFF
FRAME_SIZE = 4 + PAGE_SIZE + 4       # page_id | page | crc32(page)
COMMIT_SIZE = 12                     # 0xFFFFFFFF | frame_count | crc32(frames)

class StorageError(Exception):
    pass


REPL_MAGIC = b"MREPL001"
REPL_HEADER = 8                      # seq | frame_count
REPL_FRAME = 4 + PAGE_SIZE           # page_id | page (one CRC covers the whole record)


def read_repl_log(path):
    """Complete replication batches, oldest first: [(seq, {page_id: page})].

    A torn tail (crash inside the append) ends the stream the same way the WAL's does: the batch
    was never acknowledged, and recovery re-logs it from the WAL. Corrupt bytes anywhere else are
    a real fault and raise rather than silently shortening the log.
    """
    if not os.path.exists(path) or os.path.getsize(path) <= len(REPL_MAGIC):
        return []
    with open(path, "rb") as log:
        blob = log.read()
    if not blob.startswith(REPL_MAGIC):
        raise StorageError(f"{path}: not a MiniDB replication log")
    records = []
    offset = len(REPL_MAGIC)
    while offset + REPL_HEADER + 4 <= len(blob):
        seq, count = struct.unpack_from("<II", blob, offset)
        end = offset + REPL_HEADER + count * REPL_FRAME + 4
        if count == 0 or end > len(blob):
            break                                   # torn tail: the append never finished
        body = blob[offset:end - 4]
        if zlib.crc32(body) != struct.unpack_from("<I", blob, end - 4)[0]:
            break
        frames = {}
        for index in range(count):
            frame = offset + REPL_HEADER + index * REPL_FRAME
            page_id = struct.unpack_from("<I", blob, frame)[0]
            frames[page_id] = blob[frame + 4:frame + 4 + PAGE_SIZE]
        records.append((seq, frames))
        offset = end
    return records


CACHE_PAGES = int(os.environ.get("MINIDB_CACHE_PAGES", "512"))


class Pager:
    def __init__(self, path, readonly=False, repl_log=False):
        self.path = path
        self.readonly = readonly
        self.wal_path = path + ".wal"
        self.repl_log = bool(repl_log) and not readonly
        self.repl_path = path + ".replog"
        self.repl_seq = 0
        self._last_log_pages = None
        if self.repl_log:
            records = read_repl_log(self.repl_path)
            if records:
                self.repl_seq, self._last_log_pages = records[-1]
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        self.fh = open(path, "rb" if readonly and exists else ("r+b" if exists else "w+b"))
        # Readers share the file; a writer takes it exclusively and waits for readers to finish.
        # That is SQLite's shape for this milestone, minus WAL's non-blocking readers.
        lock = fcntl.LOCK_SH if readonly else fcntl.LOCK_EX
        timeout = float(os.environ.get("MINIDB_LOCK_TIMEOUT", "5"))
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.fh.fileno(), lock | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fh.close()
                    raise StorageError("database is locked") from None
                time.sleep(0.01)
        self.dirty = {}
        # LRU page cache (milestone 18). Disabled with MINIDB_CACHE_PAGES=0 so the before/after
        # numbers come from the same binary rather than from two checkouts.
        self.cache = OrderedDict()
        # Recovery must come first: a crash can leave the database file empty (or short) while
        # the log holds the only copy of a committed batch.
        if os.path.exists(self.wal_path) and os.path.getsize(self.wal_path) > len(WAL_MAGIC):
            self.recover()
        if os.path.getsize(path) == 0:
            self.version = VERSION
            self.page_count = 1        # page 0 is the header; storage creates the catalog tree
            self.catalog_root = 0
            self._stage_header()
            self.commit()
        else:
            try:
                self._read_header()
            except StorageError:
                # No Pager reaches the caller, so this is the only chance to release the handle.
                self.abandon()
                raise

    # -- header (page 0) ----------------------------------------------------
    def _header_page(self):
        page = bytearray(PAGE_SIZE)
        page[_OFF_MAGIC:_OFF_MAGIC + 8] = MAGIC
        page[_OFF_VERSION] = VERSION
        struct.pack_into("<H", page, _OFF_PAGE_SIZE, PAGE_SIZE)
        struct.pack_into("<I", page, _OFF_PAGE_COUNT, self.page_count)
        struct.pack_into("<I", page, _OFF_CATALOG, self.catalog_root)
        return bytes(page)

    def _stage_header(self):
        self.dirty[0] = self._header_page()

    def _append_repl_batch(self, pages):
        """Record a committed batch for shipping; returns its sequence number.

        Identical consecutive batches are skipped rather than numbered: a commit that wrote the
        same bytes as its predecessor changed no state, so the replica is already at that state.
        The dedupe also makes recovery's re-log a no-op when the crash landed after this append.
        """
        if pages == self._last_log_pages:
            return self.repl_seq
        frames = sorted(pages.items())
        body = (struct.pack("<II", self.repl_seq + 1, len(frames))
                + b"".join(struct.pack("<I", page_id) + page for page_id, page in frames))
        record = body + struct.pack("<I", zlib.crc32(body))
        fresh = not os.path.exists(self.repl_path) or os.path.getsize(self.repl_path) == 0
        with open(self.repl_path, "ab") as log:
            if fresh:
                log.write(REPL_MAGIC)
            log.write(record)
            log.flush()
            os.fsync(log.fileno())
        self.repl_seq += 1
        self._last_log_pages = {page_id: bytes(page) for page_id, page in frames}
        return self.repl_seq

    def checkpoint(self):
        """Log the whole file as one batch — the honest form of "VACUUM replaced this file".

        Page-level shipping has no incremental way to describe a file rebuild, so a full image is
        what it means. Declared residual: a crash between VACUUM's rename and this append leaves
        the log one checkpoint behind, and the next ship fails the identity check rather than
        converging silently — resolve() rebuilds the replica from the primary.
        """
        if not self.repl_log:
            return None
        # The file's own bytes: `read()` returns the payload with the checksum trailer zeroed,
        # which is right for consumers but wrong for a batch — the replica writes pages verbatim.
        with open(self.path, "rb") as handle:
            raw = handle.read()
        pages = {page_id: raw[page_id * PAGE_SIZE:(page_id + 1) * PAGE_SIZE]
                 for page_id in range(self.page_count)}
        return self._append_repl_batch(pages)

    def _read_header(self):
        data = self._read_page_bytes(0)
        if len(data) < HEADER_SIZE or data[_OFF_MAGIC:_OFF_MAGIC + 8] != MAGIC:
            raise StorageError(f"{self.path}: not a MiniDB file")
        page_size = struct.unpack_from("<H", data, _OFF_PAGE_SIZE)[0]
        if page_size != PAGE_SIZE:
            raise StorageError(f"{self.path}: header page size {page_size} != {PAGE_SIZE}")
        version = data[_OFF_VERSION]
        if version not in (VERSION, LEGACY_VERSION):
            raise StorageError(f"{self.path}: unsupported format version {version}")
        self.version = version
        self.page_count = struct.unpack_from("<I", data, _OFF_PAGE_COUNT)[0]
        self.catalog_root = struct.unpack_from("<I", data, _OFF_CATALOG)[0]

    # -- pages --------------------------------------------------------------
    def _read_page_bytes(self, page_id):
        self.fh.seek(page_id * PAGE_SIZE)
        return self.fh.read(PAGE_SIZE)

    def read(self, page_id):
        if page_id in self.dirty:
            return self.dirty[page_id]
        if page_id < 0 or page_id >= self.page_count:
            raise StorageError(f"page {page_id} out of range")
        cached = self.cache.get(page_id)
        if cached is not None:
            self.cache.move_to_end(page_id)
            return cached
        data = self._read_page_bytes(page_id)
        if len(data) != PAGE_SIZE:
            raise StorageError(f"page {page_id} truncated")
        if self.version >= 2:
            payload, stored = data[:USABLE], struct.unpack_from("<I", data, USABLE)[0]
            if zlib.crc32(payload) != stored:
                raise StorageError(f"page {page_id}: checksum mismatch")
            data = payload + b"\x00" * CHECKSUM_SIZE
        if CACHE_PAGES:
            self.cache[page_id] = data
            if len(self.cache) > CACHE_PAGES:
                self.cache.popitem(last=False)
        return data

    def write(self, page_id, data):
        if len(data) != PAGE_SIZE:
            raise StorageError(f"page {page_id}: expected {PAGE_SIZE} bytes, got {len(data)}")
        payload = bytes(data)[:USABLE].ljust(USABLE, b"\x00")
        payload = payload + struct.pack("<I", zlib.crc32(payload))
        self.dirty[page_id] = payload
        if CACHE_PAGES:
            self.cache[page_id] = payload

    def allocate(self):
        page_id = self.page_count
        self.page_count += 1
        self.dirty[page_id] = b"\x00" * PAGE_SIZE
        self._stage_header()
        return page_id

    def set_catalog_root(self, page_id):
        self.catalog_root = page_id
        self._stage_header()

    # -- commit / rollback --------------------------------------------------
    def _crash(self, site):
        """Die here if the fault injector names this site and this commit.

        The site list is the M24 coverage surface: every write, flush, fsync and truncate step in
        the commit path, plus a torn page write. A run that only ever kills one site is a lottery.
        """
        point = os.environ.get("MINIDB_CRASH_POINT", "")
        if not point:
            return
        # Legacy names from M7's two-site seam, kept so old tests and tools keep working.
        point = {"after_wal": "wal_after_fsync", "mid_apply": "db_mid_apply"}.get(point, point)
        wanted_commit = int(os.environ.get("MINIDB_CRASH_COMMIT", "1"))
        if point == site and getattr(self, "_commit_count", 0) == wanted_commit:
            os._exit(9)

    def commit(self):
        """WAL first, then the database file, then truncate. Any crash in between replays."""
        if not self.dirty:
            return
        self._commit_count = getattr(self, "_commit_count", 0) + 1
        self._stage_header()
        ordered = sorted(self.dirty.items())
        frames = b"".join(
            struct.pack("<I", page_id) + page + struct.pack("<I", zlib.crc32(page))
            for page_id, page in ordered
        )
        marker = (struct.pack("<I", WAL_COMMIT) + struct.pack("<I", len(ordered))
                  + struct.pack("<I", zlib.crc32(frames)))

        self._crash("wal_before_write")
        offset = os.path.getsize(self.wal_path) if os.path.exists(self.wal_path) else 0
        with open(self.wal_path, "ab+") as wal:
            if offset == 0:
                wal.write(WAL_MAGIC)
            wal.write(frames)
            self._crash("wal_after_write")
            wal.write(marker)
            wal.flush()
            self._crash("wal_after_flush")
            os.fsync(wal.fileno())
            self._crash("wal_after_fsync")

        self._crash("db_before_apply")
        if os.environ.get("MINIDB_CRASH_POINT") == "db_torn_page":
            page_id, page = ordered[0]
            self.fh.seek(page_id * PAGE_SIZE)
            self.fh.write(page[: PAGE_SIZE // 2])
            self.fh.flush()
            os._exit(9)
        for index, (page_id, page) in enumerate(ordered):
            self.fh.seek(page_id * PAGE_SIZE)
            if index == 0:
                self._crash("db_mid_apply")
            self.fh.write(page)
            if index == 0:
                self._crash("db_after_first_page")
        self.fh.flush()
        self._crash("db_after_flush")
        os.fsync(self.fh.fileno())
        self._crash("db_after_fsync")

        # Log the batch before the WAL is truncated: a crash in between replays the batch on the
        # next open, and the replay re-logs it (skipped when the pages are already logged).
        if self.repl_log:
            self._append_repl_batch(dict(ordered))

        self._crash("truncate_before")
        os.truncate(self.wal_path, 0)
        self._crash("truncate_after")
        self.dirty.clear()

    def rollback(self):
        """Drop uncommitted pages. The file was never touched, so there is nothing to undo."""
        self.dirty.clear()
        self.cache.clear()
        self._read_header()

    # -- recovery -----------------------------------------------------------
    def recover(self):
        """Replay complete WAL batches; a torn or uncommitted tail is discarded."""
        if not os.path.exists(self.wal_path) or os.path.getsize(self.wal_path) <= len(WAL_MAGIC):
            return False
        with open(self.wal_path, "rb") as wal:
            blob = wal.read()

        offset = len(WAL_MAGIC)
        applied = False
        while offset + COMMIT_SIZE <= len(blob):
            frames, batch = [], b""
            while True:
                if offset + 4 > len(blob):
                    self._truncate_wal()
                    return applied
                page_id = struct.unpack_from("<I", blob, offset)[0]

                if page_id == WAL_COMMIT:
                    if offset + COMMIT_SIZE > len(blob):
                        self._truncate_wal()
                        return applied
                    count, marker_crc = struct.unpack_from("<II", blob, offset + 4)
                    if count != len(frames) or zlib.crc32(batch) != marker_crc:
                        self._truncate_wal()
                        return applied
                    for frame_page_id, page in frames:
                        self.fh.seek(frame_page_id * PAGE_SIZE)
                        self.fh.write(page)
                    self.fh.flush()
                    os.fsync(self.fh.fileno())
                    if self.repl_log:
                        self._append_repl_batch(dict(frames))
                    offset += COMMIT_SIZE
                    applied = True
                    break

                if offset + FRAME_SIZE > len(blob):
                    self._truncate_wal()
                    return applied
                page = blob[offset + 4:offset + 4 + PAGE_SIZE]
                crc = struct.unpack_from("<I", blob, offset + 4 + PAGE_SIZE)[0]
                if zlib.crc32(page) != crc:
                    self._truncate_wal()
                    return applied
                frames.append((page_id, page))
                batch += blob[offset:offset + FRAME_SIZE]
                offset += FRAME_SIZE

        self._truncate_wal()
        return applied

    def _truncate_wal(self):
        if os.path.exists(self.wal_path):
            os.truncate(self.wal_path, 0)

    # -- lifecycle ----------------------------------------------------------
    def close(self):
        if self.fh.closed:
            return
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):     # already closed: close() is idempotent
            pass
        self.cache.clear()
        # A clean shutdown flushes what is pending; a crash skips this and leaves the WAL for
        # recovery. Silently dropping dirty pages here would lose data without a trace.
        self.commit()
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.fh.close()

    def abandon(self):
        """Release a handle whose owner never finished constructing.

        `close()` commits whatever is staged, which is wrong for an open that has already been
        rejected: those pages would land in a file the caller has just been told cannot be read.
        Nothing has reached the disk yet, so dropping the staged set is the whole undo, and the
        handle is released without a commit.
        """
        self.dirty.clear()
        self.cache.clear()
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):     # already closed: abandon() is idempotent
            pass
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
