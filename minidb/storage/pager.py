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
"""

import os
import struct
import zlib
from collections import OrderedDict

MAGIC = b"MiniDB01"
VERSION = 1
PAGE_SIZE = 4096

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


CACHE_PAGES = int(os.environ.get("MINIDB_CACHE_PAGES", "512"))


class Pager:
    def __init__(self, path):
        self.path = path
        self.wal_path = path + ".wal"
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        self.fh = open(path, "r+b" if exists else "w+b")
        self.dirty = {}
        # LRU page cache (milestone 18). Disabled with MINIDB_CACHE_PAGES=0 so the before/after
        # numbers come from the same binary rather than from two checkouts.
        self.cache = OrderedDict()
        # Recovery must come first: a crash can leave the database file empty (or short) while
        # the log holds the only copy of a committed batch.
        if os.path.exists(self.wal_path) and os.path.getsize(self.wal_path) > len(WAL_MAGIC):
            self.recover()
        if os.path.getsize(path) == 0:
            self.page_count = 1        # page 0 is the header; storage creates the catalog tree
            self.catalog_root = 0
            self._stage_header()
            self.commit()
        else:
            self._read_header()

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

    def _read_header(self):
        data = self._read_page_bytes(0)
        if len(data) < HEADER_SIZE or data[_OFF_MAGIC:_OFF_MAGIC + 8] != MAGIC:
            raise StorageError(f"{self.path}: not a MiniDB file")
        version = data[_OFF_VERSION]
        if version != VERSION:
            raise StorageError(f"{self.path}: unsupported format version {version}")
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
        if CACHE_PAGES:
            self.cache[page_id] = data
            if len(self.cache) > CACHE_PAGES:
                self.cache.popitem(last=False)
        return data

    def write(self, page_id, data):
        if len(data) != PAGE_SIZE:
            raise StorageError(f"page {page_id}: expected {PAGE_SIZE} bytes, got {len(data)}")
        payload = bytes(data)
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
    def commit(self):
        """WAL first, then the database file, then truncate. Any crash in between replays."""
        if not self.dirty:
            return
        self._stage_header()
        ordered = sorted(self.dirty.items())
        frames = b"".join(
            struct.pack("<I", page_id) + page + struct.pack("<I", zlib.crc32(page))
            for page_id, page in ordered
        )
        marker = (struct.pack("<I", WAL_COMMIT) + struct.pack("<I", len(ordered))
                  + struct.pack("<I", zlib.crc32(frames)))

        offset = os.path.getsize(self.wal_path) if os.path.exists(self.wal_path) else 0
        with open(self.wal_path, "ab+") as wal:
            if offset == 0:
                wal.write(WAL_MAGIC)
            wal.write(frames)
            wal.write(marker)
            wal.flush()
            os.fsync(wal.fileno())

        # Fault injection for the crash tests: die exactly where a real crash would land,
        # after the log is durable but before the database file is. MINIDB_CRASH_COMMIT picks
        # which commit in the process's life dies (1 = the very first).
        crash_point = os.environ.get("MINIDB_CRASH_POINT", "")
        crash_commit = int(os.environ.get("MINIDB_CRASH_COMMIT", "1"))
        self._commit_count = getattr(self, "_commit_count", 0) + 1
        crash_here = crash_point and self._commit_count == crash_commit
        if crash_here and crash_point == "after_wal":
            os._exit(9)

        for index, (page_id, page) in enumerate(ordered):
            self.fh.seek(page_id * PAGE_SIZE)
            self.fh.write(page)
            if crash_here and crash_point == "mid_apply" and index == 0:
                os._exit(9)
        self.fh.flush()
        os.fsync(self.fh.fileno())

        os.truncate(self.wal_path, 0)
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
        self.cache.clear()
        # A clean shutdown flushes what is pending; a crash skips this and leaves the WAL for
        # recovery. Silently dropping dirty pages here would lose data without a trace.
        self.commit()
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
