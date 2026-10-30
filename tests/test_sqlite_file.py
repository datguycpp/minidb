"""Milestone 23 tests: reading a database the reference wrote (read-only compatibility)."""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402
from minidb.storage.sqlite_file import SQLiteFile, is_sqlite_file, read_varint  # noqa: E402

SQLITE = "sqlite3"


def build(statements):
    path = os.path.join(tempfile.mkdtemp(), "fixture.db")
    subprocess.run([SQLITE, path, ";\n".join(statements) + ";"], check=True, capture_output=True)
    return path


class TestVarint(unittest.TestCase):
    def test_single_byte(self):
        self.assertEqual(read_varint(b"\x05", 0), (5, 1))

    def test_two_bytes(self):
        self.assertEqual(read_varint(b"\x81\x00", 0), (128, 2))

    def test_null_and_integers(self):
        path = build(["CREATE TABLE t (a INTEGER, b TEXT)", "INSERT INTO t VALUES (1, NULL)"])
        reader = SQLiteFile(path)
        table = [o for o in reader.schema_objects() if o["type"] == "table"][0]
        rows = list(reader.table_rows(table["root"]))
        self.assertEqual(rows, [(1, [1, None])])


class TestReadingForeignFiles(unittest.TestCase):
    def test_basic_table(self):
        path = build(["CREATE TABLE t (id INTEGER, name TEXT, score REAL)",
                      "INSERT INTO t VALUES (1, 'a', 1.5), (2, 'b', NULL)"])
        self.assertTrue(is_sqlite_file(path))
        db = Database(path)
        self.assertEqual(db.run("SELECT id, name, score FROM t ORDER BY id")[1],
                         [(1, "a", 1.5), (2, "b", None)])
        db.close()

    def test_interior_pages(self):
        path = build(["CREATE TABLE t (id INTEGER, v TEXT)",
                      "INSERT INTO t VALUES " + ", ".join(f"({i}, 'v{i}')" for i in range(1, 1201))])
        db = Database(path)
        self.assertEqual(db.run("SELECT count(*) FROM t")[1], [(1200,)])
        self.assertEqual(db.run("SELECT v FROM t WHERE id = 900")[1], [("v900",)])
        db.close()

    def test_overflow_record(self):
        body = "x" * 5000
        path = build(["CREATE TABLE t (id INTEGER, body TEXT)",
                      f"INSERT INTO t VALUES (1, '{body}')"])
        db = Database(path)
        self.assertEqual(db.run("SELECT length(body) FROM t")[1], [(5000,)])
        db.close()

    def test_unicode_text(self):
        path = build(["CREATE TABLE t (s TEXT)", "INSERT INTO t VALUES ('café'), ('汉字')"])
        db = Database(path)
        self.assertEqual(db.run("SELECT s FROM t ORDER BY s")[1], [("café",), ("汉字",)])
        db.close()

    def test_indexes_from_the_foreign_schema_are_loaded(self):
        path = build(["CREATE TABLE t (a INTEGER, b TEXT)",
                      "INSERT INTO t VALUES " + ", ".join(f"({i % 10}, 'b{i % 3}')" for i in range(1, 201)),
                      "CREATE INDEX idx_a ON t (a)"])
        db = Database(path)
        self.assertIn("idx_a", {index.name for index in db.tables["t"].indexes.values()})
        self.assertEqual(db.run("SELECT count(*) FROM t WHERE a = 3")[1], [(20,)])
        db.close()

    def test_views_are_read_from_sqlite_master(self):
        path = build(["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (1), (2)",
                      "CREATE VIEW v AS SELECT id FROM t WHERE id > 1"])
        db = Database(path)
        self.assertEqual(db.run("SELECT * FROM v")[1], [(2,)])
        db.close()

    def test_writes_are_refused(self):
        path = build(["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (1)"])
        db = Database(path)
        with self.assertRaises(ExecError) as ctx:
            db.run("INSERT INTO t VALUES (2)")
        self.assertEqual(str(ctx.exception), "attempt to write a readonly database")
        db.close()

    def test_numeric_text_survives_as_stored(self):
        path = build(["CREATE TABLE t (n INTEGER, s TEXT)",
                      "INSERT INTO t VALUES (1, '5'), (2, '3.0')"])
        db = Database(path)
        self.assertEqual(db.run("SELECT typeof(s), s FROM t ORDER BY n")[1],
                         [("text", "5"), ("text", "3.0")],
                         "affinity was already applied by SQLite; reading must not re-coerce")
        db.close()


class DifferentialFileCompat(unittest.TestCase):
    CASES = [
        ["CREATE TABLE t (id INTEGER, name TEXT)", "INSERT INTO t VALUES (1,'a'),(2,'b')",
         "SELECT id, name FROM t ORDER BY id"],
        ["CREATE TABLE t (id INTEGER, v TEXT)",
         "INSERT INTO t VALUES " + ", ".join(f"({i}, 'v{i}')" for i in range(1, 501)),
         "SELECT count(*), max(id) FROM t"],
        ["CREATE TABLE t (name TEXT COLLATE NOCASE)", "INSERT INTO t VALUES ('Alpha'),('beta')",
         "SELECT name FROM t ORDER BY name"],
        ["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (1),(2)",
         "CREATE VIEW v AS SELECT id FROM t WHERE id > 1", "SELECT * FROM v"],
        ["CREATE TABLE t (id INTEGER, v INTEGER)",
         "INSERT INTO t VALUES " + ", ".join(f"({i}, {i if i % 2 else 'NULL'})" for i in range(1, 51)),
         "SELECT count(*), count(v), sum(v) FROM t"],
    ]

    def test_cases_match_the_reference(self):
        for statements in self.CASES:
            path = build(statements[:-1])
            query = statements[-1]
            expected = bc.run_sqlite([statements])
            db = Database(path)
            try:
                columns, rows = db.run(query)
                actual = {"columns": list(columns),
                          "rows": bc.normalise_values([dict(zip(columns, row)) for row in rows])}
            except Exception as exc:
                actual = {"error": str(exc)}
            finally:
                db.close()
            ok, detail = bc.compare(expected, actual)
            self.assertTrue(ok, f"{query} -> {detail}")


if __name__ == "__main__":
    unittest.main()
