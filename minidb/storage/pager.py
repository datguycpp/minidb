"""Fixed-size page file (milestone 3).

Layout, chosen once and written down because changing it later means a migration:

    page 0     header: magic "MiniDB01" | version | page_size | page_count | catalog_root
    page 1..n  B+tree nodes and catalog rows, addressed by page number

Every page is 4096 bytes. Writes go straight to the file (write-through); fsync happens on
close. Crash safety is deliberately NOT claimed here — the write-ahead log is milestone 7, and
milestone 3 only promises that a cleanly closed database reopens intact.
"""

import os
import struct

MAGIC = b"MiniDB01"
VERSION = 1
PAGE_SIZE = 4096

# Page 0 header field offsets.
_OFF_MAGIC = 0
_OFF_VERSION = 8
_OFF_PAGE_SIZE = 9
_OFF_PAGE_COUNT = 11
_OFF_CATALOG = 15
HEADER_SIZE = 19


class StorageError(Exception):
    pass


class Pager:
    def __init__(self, path):
        self.path = path
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        self.fh = open(path, "r+b" if not new_file else "w+b")
        if new_file:
            # Page 0 belongs to the header; the catalog tree is created by DiskStorage, which
            # owns page structure. catalog_root == 0 means "empty database, needs a root".
            self.page_count = 1
            self.catalog_root = 0
            self._write_header()
        else:
            self._read_header()

    # -- header -------------------------------------------------------------
    def _read_header(self):
        self.fh.seek(0)
        header = self.fh.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE or header[_OFF_MAGIC:_OFF_MAGIC + 8] != MAGIC:
            raise StorageError(f"{self.path}: not a MiniDB file")
        version = header[_OFF_VERSION]
        if version != VERSION:
            raise StorageError(f"{self.path}: unsupported format version {version}")
        self.page_count = struct.unpack_from("<I", header, _OFF_PAGE_COUNT)[0]
        self.catalog_root = struct.unpack_from("<I", header, _OFF_CATALOG)[0]

    def _write_header(self):
        header = bytearray(HEADER_SIZE)
        header[_OFF_MAGIC:_OFF_MAGIC + 8] = MAGIC
        header[_OFF_VERSION] = VERSION
        struct.pack_into("<H", header, _OFF_PAGE_SIZE, PAGE_SIZE)
        struct.pack_into("<I", header, _OFF_PAGE_COUNT, self.page_count)
        struct.pack_into("<I", header, _OFF_CATALOG, self.catalog_root)
        self.fh.seek(0)
        self.fh.write(bytes(header))

    # -- pages --------------------------------------------------------------
    def allocate(self):
        page_id = self.page_count
        self.page_count += 1
        self.fh.seek(page_id * PAGE_SIZE)
        self.fh.write(b"\x00" * PAGE_SIZE)
        self._write_header()
        return page_id

    def read(self, page_id):
        if page_id <= 0 or page_id >= self.page_count:
            raise StorageError(f"page {page_id} out of range")
        self.fh.seek(page_id * PAGE_SIZE)
        data = self.fh.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise StorageError(f"page {page_id} truncated")
        return data

    def write(self, page_id, data):
        if len(data) != PAGE_SIZE:
            raise StorageError(f"page {page_id}: expected {PAGE_SIZE} bytes, got {len(data)}")
        self.fh.seek(page_id * PAGE_SIZE)
        self.fh.write(data)
        self.fh.flush()

    def set_catalog_root(self, page_id):
        self.catalog_root = page_id
        self._write_header()

    def close(self):
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
