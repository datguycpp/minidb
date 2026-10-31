"""B+tree over fixed-size pages (milestone 3).

Layout: leaves hold (rowid, payload); interior nodes hold a leftmost child plus
(separator, child) pairs, where the child under separator k holds keys >= k and the child
before it holds keys < k. That is the classic "leftmost pointer + (key, right) pairs" form,
chosen because splitting then means promoting a separator and pointing at the new right page —
no key duplication, no special case for the far right.

Page layout:

    0        node type: 1 = interior, 2 = leaf
    1..3     cell count (uint16)
    3..7     leftmost child (uint32, interior only; 0 for leaves)
    8..      cells in key order

    leaf cell       int64 key | uint32 payload length | payload
    interior cell   int64 separator | uint32 child page

No deletes yet: DELETE is milestone 16, so there is no merge-on-underflow code pretending to
be tested. Nodes are rewritten whole on modification; dirty-page tracking is M18.
"""

import struct

from .pager import PAGE_SIZE, USABLE, Pager, StorageError

LEAF = 2
INTERIOR = 1
HEADER = 8
OVERFLOW = 0xFFFFFFFF
INLINE_LIMIT = 1000            # payloads above this go to an overflow chain
CHUNK = USABLE - 4          # each overflow page: 4-byte next id + chunk


class Node:
    __slots__ = ("page_id", "kind", "cells", "left_child")

    def __init__(self, page_id, kind, cells=None, left_child=0):
        self.page_id = page_id
        self.kind = kind
        self.cells = cells or []          # leaf: [(key, payload)]; interior: [(separator, child)]
        self.left_child = left_child


class BTree:
    def __init__(self, pager: Pager):
        self.pager = pager

    # -- page (de)serialisation --------------------------------------------
    def load(self, page_id):
        try:
            return self._load(page_id)
        except (struct.error, IndexError, ValueError) as exc:
            # A damaged page must surface as our typed storage error, not a struct traceback.
            raise StorageError(f"page {page_id}: corrupt b-tree page ({exc})") from None

    def _load(self, page_id):
        data = self.pager.read(page_id)
        kind = data[0]
        count = struct.unpack_from("<H", data, 1)[0]
        left = struct.unpack_from("<I", data, 3)[0]
        cells = []
        offset = HEADER
        for _ in range(count):
            key = struct.unpack_from("<q", data, offset)[0]
            offset += 8
            if kind == LEAF:
                length = struct.unpack_from("<I", data, offset)[0]
                offset += 4
                if length == OVERFLOW:
                    first = struct.unpack_from("<I", data, offset)[0]
                    total = struct.unpack_from("<I", data, offset + 4)[0]
                    offset += 8
                    cells.append((key, self._read_overflow(first, total)))
                else:
                    cells.append((key, data[offset:offset + length]))
                    offset += length
            else:
                cells.append((key, struct.unpack_from("<I", data, offset)[0]))
                offset += 4
        return Node(page_id, kind, cells, left)

    def save(self, node):
        buffer = bytearray(PAGE_SIZE)
        buffer[0] = node.kind
        struct.pack_into("<H", buffer, 1, len(node.cells))
        struct.pack_into("<I", buffer, 3, node.left_child)
        offset = HEADER
        for cell in node.cells:
            if node.kind == LEAF:
                key, payload = cell
                if len(payload) > INLINE_LIMIT:
                    first = self._write_overflow(payload)
                    struct.pack_into("<q", buffer, offset, key)
                    struct.pack_into("<I", buffer, offset + 8, OVERFLOW)
                    struct.pack_into("<I", buffer, offset + 12, first)
                    struct.pack_into("<I", buffer, offset + 16, len(payload))
                    offset += 20
                    continue
                if offset + 12 + len(payload) > USABLE:
                    raise StorageError("row too large for a page even inlined")
                struct.pack_into("<q", buffer, offset, key)
                struct.pack_into("<I", buffer, offset + 8, len(payload))
                offset += 12
                buffer[offset:offset + len(payload)] = payload
                offset += len(payload)
            else:
                key, child = cell
                if offset + 12 > USABLE:
                    raise StorageError("node overflow: interior cell does not fit")
                struct.pack_into("<q", buffer, offset, key)
                struct.pack_into("<I", buffer, offset + 8, child)
                offset += 12
        self.pager.write(node.page_id, bytes(buffer))

    # -- overflow chains ----------------------------------------------------
    def _write_overflow(self, payload):
        pages = [payload[i:i + CHUNK] for i in range(0, len(payload), CHUNK)]
        ids = [self.pager.allocate() for _ in pages]
        for index, (page_id, chunk) in enumerate(zip(ids, pages)):
            buffer = bytearray(PAGE_SIZE)
            nxt = ids[index + 1] if index + 1 < len(ids) else 0
            struct.pack_into("<I", buffer, 0, nxt)
            buffer[4:4 + len(chunk)] = chunk
            self.pager.write(page_id, bytes(buffer))
        return ids[0]

    def _read_overflow(self, first, total):
        chunks = []
        page_id = first
        while page_id:
            data = self.pager.read(page_id)
            nxt = struct.unpack_from("<I", data, 0)[0]
            chunks.append(data[4:USABLE])
            page_id = nxt
        return b"".join(chunks)[:total]

    @staticmethod
    def _fits(node, extra=None):
        total = HEADER
        for cell in (node.cells + ([extra] if extra else [])):
            if node.kind == LEAF:
                size = len(cell[1])
                total += 12 + (size if size <= INLINE_LIMIT else 8)
            else:
                total += 12
        return total <= USABLE

    # -- creation -----------------------------------------------------------
    def create(self):
        page_id = self.pager.allocate()
        self.save(Node(page_id, LEAF, [], 0))
        return page_id

    # -- reads --------------------------------------------------------------
    @staticmethod
    def _child_for(node, key):
        if not node.cells or key < node.cells[0][0]:
            return node.left_child
        chosen = node.left_child
        for separator, child in node.cells:
            if key < separator:
                break
            chosen = child
        return chosen

    def search(self, root, key):
        node = self.load(root)
        while node.kind == INTERIOR:
            node = self.load(self._child_for(node, key))
        for cell_key, payload in node.cells:
            if cell_key == key:
                return payload
            if cell_key > key:
                break
        return None

    def scan(self, root):
        out = []

        def walk(page_id):
            node = self.load(page_id)
            if node.kind == LEAF:
                out.extend(node.cells)
                return
            walk(node.left_child)
            for _, child in node.cells:
                walk(child)

        walk(root)
        return out

    def max_key(self, root):
        node = self.load(root)
        while node.kind == INTERIOR:
            node = self.load(node.cells[-1][1] if node.cells else node.left_child)
        return node.cells[-1][0] if node.cells else 0

    # -- writes -------------------------------------------------------------
    def insert(self, root, key, payload):
        """Insert or replace. Returns the tree's (possibly new) root page."""
        split = self._insert(root, key, payload)
        if split is None:
            return root
        separator, right_page = split
        new_root = self.pager.allocate()
        self.save(Node(new_root, INTERIOR, [(separator, right_page)], left_child=root))
        return new_root

    def _insert(self, page_id, key, payload):
        node = self.load(page_id)

        if node.kind == LEAF:
            for index, (cell_key, _) in enumerate(node.cells):
                if cell_key == key:
                    node.cells[index] = (key, payload)
                    self.save(node)
                    return None
                if cell_key > key:
                    node.cells.insert(index, (key, payload))
                    break
            else:
                node.cells.append((key, payload))
            if self._fits(node):
                self.save(node)
                return None
            return self._split_leaf(node)

        child_page = self._child_for(node, key)
        split = self._insert(child_page, key, payload)
        if split is None:
            return None

        separator, right_page = split
        for index, (cell_key, _) in enumerate(node.cells):
            if separator < cell_key:
                node.cells.insert(index, (separator, right_page))
                break
        else:
            node.cells.append((separator, right_page))
        if self._fits(node):
            self.save(node)
            return None
        return self._split_interior(node)

    def _split_leaf(self, node):
        middle = len(node.cells) // 2
        right_cells = node.cells[middle:]
        node.cells = node.cells[:middle]
        self.save(node)
        right = Node(self.pager.allocate(), LEAF, right_cells, 0)
        self.save(right)
        return right_cells[0][0], right.page_id

    def _split_interior(self, node):
        middle = len(node.cells) // 2
        separator, promoted_child = node.cells[middle]
        right_cells = node.cells[middle + 1:]
        left_cells = node.cells[:middle]
        node.cells = left_cells
        self.save(node)
        right = Node(self.pager.allocate(), INTERIOR, right_cells, left_child=promoted_child)
        self.save(right)
        return separator, right.page_id
