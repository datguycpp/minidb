"""Read a SQLite 3 database file (milestone 23, read compatibility only).

The scope decision is on paper in docs/m23-file-compat-spec.md: MiniDB reads files the reference
wrote, and never writes them. That means this module parses SQLite's on-disk format — header,
table b-trees, record serial types, overflow chains — and hands plain Python values to the engine,
which then answers queries with its own executor.

Everything here follows https://www.sqlite.org/fileformat2.html; section numbers in comments point
at the paragraph that decides each rule.
"""

import struct


class SQLiteFileError(Exception):
    pass


MAGIC = b"SQLite format 3\x00"

LEAF_TABLE = 0x0D
INTERIOR_TABLE = 0x05
LEAF_INDEX = 0x0A
INTERIOR_INDEX = 0x02


def is_sqlite_file(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == MAGIC
    except OSError:
        return False


def read_varint(data, offset):
    """SQLite's variable-length integer (fileformat2 §2.1): up to 9 bytes, big-endian groups."""
    value = 0
    for index in range(9):
        byte = data[offset + index]
        if index == 8:
            return (value << 8) | byte, offset + 9
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset + index + 1
    raise SQLiteFileError("varint too long")


class SQLiteFile:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as handle:
            self.data = handle.read()
        if len(self.data) < 100 or self.data[:16] != MAGIC:
            raise SQLiteFileError(f"{path}: not a SQLite database")
        raw_page_size = struct.unpack_from(">H", self.data, 16)[0]
        self.page_size = 65536 if raw_page_size == 1 else raw_page_size
        if self.page_size < 512 or self.page_size & (self.page_size - 1):
            raise SQLiteFileError(f"{path}: bad page size {self.page_size}")
        self.usable = self.page_size - self.data[20]
        self.encoding = struct.unpack_from(">I", self.data, 56)[0] or 1

    # -- pages --------------------------------------------------------------
    def page(self, number):
        start = (number - 1) * self.page_size
        return self.data[start:start + self.page_size]

    def page_header_offset(self, number):
        return 100 if number == 1 else 0

    # -- records ------------------------------------------------------------
    def decode_record(self, payload):
        header_size, offset = read_varint(payload, 0)
        types = []
        while offset < header_size:
            serial, offset = read_varint(payload, offset)
            types.append(serial)
        values = []
        body = header_size
        for serial in types:
            value, body = self.decode_value(payload, body, serial)
            values.append(value)
        return values

    def decode_value(self, payload, offset, serial):
        if serial == 0:
            return None, offset
        if serial == 1:
            return struct.unpack_from(">b", payload, offset)[0], offset + 1
        if serial == 2:
            return struct.unpack_from(">h", payload, offset)[0], offset + 2
        if serial == 3:
            raw = payload[offset:offset + 3]
            value = int.from_bytes(raw, "big", signed=True)
            return value, offset + 3
        if serial == 4:
            return struct.unpack_from(">i", payload, offset)[0], offset + 4
        if serial == 5:
            raw = payload[offset:offset + 6]
            return int.from_bytes(raw, "big", signed=True), offset + 6
        if serial == 6:
            return struct.unpack_from(">q", payload, offset)[0], offset + 8
        if serial == 7:
            return struct.unpack_from(">d", payload, offset)[0], offset + 8
        if serial == 8:
            return 0, offset
        if serial == 9:
            return 1, offset
        if serial >= 12:
            length = (serial - 12) // 2
            raw = payload[offset:offset + length]
            if serial % 2 == 0:
                return bytes(raw), offset + length
            return self.decode_text(raw), offset + length
        raise SQLiteFileError(f"reserved serial type {serial}")

    def decode_text(self, raw):
        if self.encoding == 1:
            return bytes(raw).decode("utf-8", "surrogateescape")
        if self.encoding == 2:
            return bytes(raw).decode("utf-16-le", "surrogatepass")
        return bytes(raw).decode("utf-16-be", "surrogatepass")

    # -- overflow -----------------------------------------------------------
    def read_payload(self, payload_start, payload_size, local_size):
        local = self.data[payload_start:payload_start + local_size]
        if local_size >= payload_size:
            return local
        chunks = [local]
        next_page = struct.unpack_from(">I", self.data, payload_start + local_size)[0]
        remaining = payload_size - local_size
        while next_page and remaining > 0:
            page = self.page(next_page)
            next_page = struct.unpack_from(">I", page, 0)[0]
            chunk = page[4:4 + min(remaining, self.usable - 4)]
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def local_payload_size(self, payload_size):
        """fileformat2 §2.6: how much of a record stays on the b-tree page."""
        max_local = self.usable - 35
        if payload_size <= max_local:
            return payload_size
        minimum = ((self.usable - 12) * 32 // 255) - 23
        candidate = minimum + ((payload_size - minimum) % (self.usable - 4))
        return candidate if candidate <= max_local else minimum

    # -- b-tree traversal ---------------------------------------------------
    def table_rows(self, root_page):
        """Yield (rowid, values) for every row under a table b-tree root."""
        stack = [root_page]
        while stack:
            number = stack.pop()
            page = self.page(number)
            header = self.page_header_offset(number)
            kind = page[header]
            cell_count = struct.unpack_from(">H", page, header + 3)[0]
            pointer_base = header + (12 if kind in (INTERIOR_TABLE, INTERIOR_INDEX) else 8)
            pointers = [struct.unpack_from(">H", page, pointer_base + 2 * i)[0]
                        for i in range(cell_count)]
            if kind == INTERIOR_TABLE:
                children = []
                for pointer in pointers:
                    child = struct.unpack_from(">I", page, pointer)[0]
                    children.append(child)
                right = struct.unpack_from(">I", page, header + 8)[0]
                children.append(right)
                stack.extend(reversed(children))
            elif kind == LEAF_TABLE:
                for pointer in pointers:
                    payload_size, offset = read_varint(page, pointer)
                    rowid, offset = read_varint(page, offset)
                    local = self.local_payload_size(payload_size)
                    payload = self.read_payload_id(number, offset, payload_size, local)
                    yield rowid, self.decode_record(payload)
            else:
                raise SQLiteFileError(f"page {number}: unexpected page type {kind}")

    def read_payload_id(self, page_number, offset, payload_size, local_size):
        """Overflow pointers live on the b-tree page; the payload may continue elsewhere."""
        start = (page_number - 1) * self.page_size + offset
        return self.read_payload(start, payload_size, local_size)

    # -- schema -------------------------------------------------------------
    def schema_objects(self):
        objects = []
        for _, values in self.table_rows(1):
            if len(values) < 5:
                continue
            kind, name, table_name, root, sql = values[:5]
            objects.append({"type": kind, "name": name, "tbl_name": table_name,
                            "root": root, "sql": sql})
        return objects
