"""Milestone 3 tests: pages, B+tree, row encoding, and a database that survives close/reopen.

Persistence is invisible to the differential corpus (the runner opens an in-memory database and
never reopens), so these tests — and tools/durability_check.py — are the only evidence that
storage works at all.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.engine import Database, ExecError
from minidb.storage import record
from minidb.storage.btree import BTree
from minidb.storage.pager import MAGIC, PAGE_SIZE, Pager, StorageError


class TempDb(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test.db")

    def tearDown(self):
        self.dir.cleanup()


class TestPager(TempDb):
    def test_new_file_has_magic_header(self):
        with open(self.path, "wb") as fh:
            fh.write(b"")
        pager = Pager(self.path)
        pager.close()
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(8), MAGIC)

    def test_reopen_reads_page_count(self):
        pager = Pager(self.path)
        pager.allocate()
        pager.allocate()
        declared = pager.page_count
        pager.close()
        again = Pager(self.path)
        self.assertEqual(again.page_count, declared)
        again.close()

    def test_foreign_file_is_rejected(self):
        with open(self.path, "wb") as fh:
            fh.write(b"not a database" * 400)
        with self.assertRaises(StorageError) as ctx:
            Pager(self.path)
        self.assertIn("not a MiniDB file", str(ctx.exception))


class TestRecord(unittest.TestCase):
    def test_roundtrip_all_types(self):
        values = (None, 42, -7, 3.5, "hello", b"\x00\x01", "")
        self.assertEqual(record.decode(record.encode(values)), values)

    def test_text_stored_as_utf8(self):
        encoded = record.encode(("héllo →",))
        self.assertIn("héllo →".encode("utf-8"), encoded)

    def test_unicode_roundtrip(self):
        self.assertEqual(record.decode(record.encode(("日本語",))), ("日本語",))


class TestBTree(TempDb):
    def setUp(self):
        super().setUp()
        self.pager = Pager(self.path)
        self.tree = BTree(self.pager)

    def tearDown(self):
        self.pager.close()
        super().tearDown()

    def test_empty_tree(self):
        root = self.tree.create()
        self.assertEqual(self.tree.scan(root), [])
        self.assertIsNone(self.tree.search(root, 1))

    def test_insert_and_search(self):
        root = self.tree.create()
        root = self.tree.insert(root, 5, b"five")
        self.assertEqual(self.tree.search(root, 5), b"five")
        self.assertIsNone(self.tree.search(root, 6))

    def test_scan_is_key_ordered_after_splits(self):
        root = self.tree.create()
        keys = list(range(1000, 0, -1))       # reverse order forces leaf splits
        for key in keys:
            root = self.tree.insert(root, key, f"value-{key}".encode())
        entries = self.tree.scan(root)
        self.assertEqual([k for k, _ in entries], sorted(keys))
        self.assertEqual(len(entries), 1000)

    def test_every_key_findable_after_splits(self):
        root = self.tree.create()
        for key in range(1, 501):
            root = self.tree.insert(root, key, str(key).encode())
        for key in range(1, 501):
            self.assertEqual(self.tree.search(root, key), str(key).encode(), key)

    def test_replace_existing_key(self):
        root = self.tree.create()
        root = self.tree.insert(root, 1, b"old")
        root = self.tree.insert(root, 1, b"new")
        self.assertEqual(self.tree.scan(root), [(1, b"new")])

    def test_tree_grows_beyond_one_page(self):
        root = self.tree.create()
        payload = b"x" * 100
        for key in range(1, 201):
            root = self.tree.insert(root, key, payload)
        self.assertGreater(self.pager.page_count, 3, "200 rows of 100 bytes must not fit one page")
        self.assertEqual(len(self.tree.scan(root)), 200)


class TestDiskDatabase(TempDb):
    def test_data_survives_reopen(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (id INTEGER, name TEXT)")
        db.run("INSERT INTO t VALUES (1, 'alpha')")
        db.run("INSERT INTO t VALUES (2, 'beta')")
        db.close()

        again = Database(self.path)
        self.assertEqual(again.run("SELECT * FROM t"), (["id", "name"], [(1, "alpha"), (2, "beta")]))
        again.close()

    def test_schema_survives_reopen_including_constraints(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL, n INTEGER CHECK (n > 0))")
        db.close()

        again = Database(self.path)
        with self.assertRaises(ExecError) as ctx:
            again.run("INSERT INTO t VALUES (1, NULL, 1)")
        self.assertEqual(str(ctx.exception), "NOT NULL constraint failed: t.name")
        with self.assertRaises(ExecError) as ctx:
            again.run("INSERT INTO t VALUES (1, 'x', 0)")
        self.assertEqual(str(ctx.exception), "CHECK constraint failed: n > 0")
        again.close()

    def test_multiple_tables(self):
        db = Database(self.path)
        db.run("CREATE TABLE a (x INTEGER)")
        db.run("CREATE TABLE b (y TEXT)")
        db.run("INSERT INTO a VALUES (1)")
        db.run("INSERT INTO b VALUES ('one')")
        db.close()

        again = Database(self.path)
        self.assertEqual(again.run("SELECT * FROM a")[1], [(1,)])
        self.assertEqual(again.run("SELECT * FROM b")[1], [("one",)])
        again.close()

    def test_writes_after_reopen(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (id INTEGER)")
        db.close()
        for round_number in range(3):
            db = Database(self.path)
            db.run(f"INSERT INTO t VALUES ({round_number})")
            db.close()
        db = Database(self.path)
        self.assertEqual(db.run("SELECT * FROM t")[1], [(0,), (1,), (2,)])
        db.close()

    def test_duplicate_create_after_reopen(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (id INTEGER)")
        db.close()
        again = Database(self.path)
        with self.assertRaises(ExecError) as ctx:
            again.run("CREATE TABLE t (id INTEGER)")
        self.assertEqual(str(ctx.exception), "table t already exists")
        again.close()

    def test_oversized_row_uses_overflow_pages(self):
        # This was a refusal guard ("row too large") until M19 added overflow chains; the
        # invariant now is that a row bigger than one page stores and reads back intact.
        db = Database(self.path)
        db.run("CREATE TABLE t (body TEXT)")
        db.run(f"INSERT INTO t VALUES ('{'x' * 8000}')")
        self.assertEqual(db.run("SELECT length(body) FROM t")[1], [(8000,)])
        db.close()
        again = Database(self.path)
        self.assertEqual(again.run("SELECT length(body) FROM t")[1], [(8000,)])
        again.close()

    def test_file_page_count_is_reasonable(self):
        db = Database(self.path)
        db.run("CREATE TABLE t (id INTEGER)")
        for i in range(100):
            db.run(f"INSERT INTO t VALUES ({i})")
        db.close()
        size = os.path.getsize(self.path)
        self.assertEqual(size % PAGE_SIZE, 0)
        self.assertGreater(size, PAGE_SIZE)

    def test_in_memory_database_is_unaffected(self):
        db = Database()
        db.run("CREATE TABLE t (id INTEGER)")
        db.run("INSERT INTO t VALUES (1)")
        self.assertEqual(db.run("SELECT * FROM t")[1], [(1,)])
        self.assertIsNone(db.storage)


if __name__ == "__main__":
    unittest.main()
