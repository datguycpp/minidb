"""Milestone 31 tests: the Python binding (client) against a live server."""

import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.client import Client, ServerError  # noqa: E402
from minidb.engine import Database, ExecError  # noqa: E402
from minidb.server import serve  # noqa: E402


class TestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(tempfile.mkdtemp(), "client.db")
        cls.server = serve(cls.path, port=0)
        cls.host, cls.port = cls.server.server_address
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.client = Client(self.host, self.port)

    def tearDown(self):
        self.client.close()

    def test_round_trip_matches_local_execution(self):
        statements = ["CREATE TABLE IF NOT EXISTS rt (a INTEGER, b TEXT)",
                      "DELETE FROM rt",
                      "INSERT INTO rt VALUES (1, 'x'), (2, 'y'), (3, NULL)"]
        for statement in statements:
            self.client.execute(statement)
        # A local *in-memory* database, because the server holds M25's exclusive lock on the file.
        local = Database()
        for statement in statements:
            local.run(statement)
        for query in ["SELECT count(*) FROM rt", "SELECT a, b FROM rt ORDER BY a",
                      "SELECT sum(a), avg(a) FROM rt"]:
            remote_columns, remote_rows = self.client.execute(query)
            local_columns, local_rows = local.run(query)
            self.assertEqual(remote_columns, list(local_columns), query)
            self.assertEqual(remote_rows, [tuple(row) for row in local_rows], query)
        local.close()

    def test_sql_error_is_typed_and_word_for_word(self):
        with self.assertRaises(ServerError) as remote:
            self.client.execute("SELECT * FROM missing")
        local = Database()
        with self.assertRaises(ExecError) as local_error:
            local.run("SELECT * FROM missing")
        local.close()
        self.assertEqual(str(remote.exception), str(local_error.exception))

    def test_rows_are_tuples_and_nulls_survive(self):
        self.client.execute("CREATE TABLE IF NOT EXISTS n (v INTEGER)")
        self.client.execute("DELETE FROM n")
        self.client.execute("INSERT INTO n VALUES (1), (NULL)")
        columns, rows = self.client.execute("SELECT v FROM n ORDER BY v")
        self.assertEqual(columns, ["v"])
        self.assertEqual(rows, [(None,), (1,)])

    def test_readme_example_shape(self):
        # The example in README.md, reduced to what a test can assert without a fixed port.
        client = Client(self.host, self.port)
        client.execute("CREATE TABLE IF NOT EXISTS demo (a INTEGER)")
        client.execute("DELETE FROM demo")
        client.execute("INSERT INTO demo VALUES (1), (2)")
        columns, rows = client.execute("SELECT sum(a) FROM demo")
        self.assertEqual(columns, ["sum(a)"])
        self.assertEqual(rows, [(3,)])
        client.close()

    def test_post_shutdown_persistence_proves_disk_and_lock_release(self):
        # Paired assertion for the in-memory parity test: after server_close(),
        # open the file directly — proves the lock was released AND rows hit disk.
        path = os.path.join(tempfile.mkdtemp(), "persist.db")
        srv = serve(path, port=0)
        host, port = srv.server_address
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        client = Client(host, port)
        client.execute("CREATE TABLE p (a INTEGER)")
        client.execute("INSERT INTO p VALUES (1), (2)")
        client.close()
        srv.shutdown()
        srv.server_close()
        check = Database(path)
        self.assertEqual(check.run("SELECT sum(a) FROM p")[1], [(3,)])
        check.close()


if __name__ == "__main__":
    unittest.main()
