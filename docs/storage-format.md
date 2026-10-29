# On-disk format (milestone 3)

Written down because the format is the one thing in this project that is expensive to change
later: a file written today must still open after M6 (transactions), M7 (WAL), M18 (page cache)
and M20 (overflow pages). Everything below is deliberate, including what is absent.

## Pages

Every page is **4096 bytes**. Page 0 is the header and is never a tree node.

```
page 0   offset 0   magic "MiniDB01"
         offset 8   format version (1 byte)
         offset 9   page size (uint16, 4096)
         offset 11  page count (uint32)
         offset 15  catalog root page (uint32)
```

A page count of 1 with `catalog_root == 0` means "new file, no catalog yet"; the storage layer
allocates the catalog tree on first open.

## B+tree nodes

Both catalog and table data are B+trees keyed by int64. Pages are either interior or leaf:

```
0        node type: 1 interior, 2 leaf
1..3     cell count (uint16)
3..7     leftmost child (uint32; interior only, 0 on leaves)
8..      cells in ascending key order
```

- **leaf cell:** `int64 key | uint32 payload length | payload`
- **interior cell:** `int64 separator | uint32 child` — the child holds keys `>= separator`;
  the child to the left of it holds keys `< separator`, and the leftmost child holds everything
  below the first separator.

Splitting promotes a separator and creates a right page; there is no key duplication and no
special case for the far right, which is why this representation was chosen over
"key + left child" pairs.

## Catalog

The catalog is a B+tree keyed by catalog rowid. Each row is JSON:

```json
{"name": "t", "sql": "CREATE TABLE t (id INTEGER, name TEXT)", "root": 4, "next_rowid": 7}
```

**Storing the original CREATE statement rather than a serialised AST** is the central
trade-off: reopening reparses SQL (one code path for schema semantics, including CHECK
expressions), at the cost of a parse per table per open. It also means a future syntax
extension needs no migration.

## Row encoding

`uint16 column count`, then per value a tag byte: `0` NULL, `1` int64, `2` float64, `3` uint32
length + UTF-8, `4` uint32 length + bytes. No type affinity is applied — a TEXT value in an
INTEGER column stays TEXT until M17.

## Deliberately absent, and owned by a later milestone

| Not here | Why | Milestone |
| --- | --- | --- |
| Write-ahead log, crash recovery | writes are write-through; a clean `close()` is the only promise | 7 |
| Page cache, dirty tracking | every modification rewrites the whole page | 18 |
| Free-space management / freelist | pages are never freed; DELETE does not exist yet | 16, 19 |
| Overflow pages | a row larger than a page raises "row too large" | 20 |
| Deletes / rebalancing | no DELETE statement exists, so no merge-on-underflow code is written | 16 |
| Type affinity | values are stored exactly as given | 17 |

## Costs measured at this milestone

| Workload | Time |
| --- | --- |
| 1000 inserts, in-memory | 0.15 s (≈6,450/s) |
| 1000 inserts, on disk | 0.57 s (≈1,740/s) |
| 1500-row scan from disk | 0.008 s |
| 500 rows + 4 columns | 15 pages (60 KiB) |

Each insert rewrites the affected leaf and the catalog row (which carries `next_rowid` and the
tree root). That is the obvious inefficiency to attack in M18, and it is stated here rather
than discovered later.
