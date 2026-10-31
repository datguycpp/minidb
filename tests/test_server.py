"""Milestone 30 tests: the network server, judged against local in-process answers.

There is no wire protocol to be compatible with, so the acceptance is equality: what a client gets
over TCP must match what the same statements return through a local Database.
"""

import json
import os
import socket
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402
from minidb.server import serve  # noqa: E402


def client(host, port, statements):
    sock = socket.create_connection((host, port), timeout=5)
    stream = sock.makefile("rwb")
    responses = []
    try:
        for sql in statements:
            stream.write((json.dumps({"sql": sql}) + "\n").encode())
            stream.flush()
            responses.append(json.loads(stream.readline().decode()))
    finally:
        sock.close()
    return responses


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve(port=0)
        cls.host, cls.port = cls.server.server_address
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_ddl_returns_no_rows(self):
        responses = client(self.host, self.port, ["CREATE TABLE IF NOT EXISTS setup_t (a INTEGER)",
                                                  "INSERT INTO setup_t VALUES (1)"])
        self.assertEqual(responses[0]["columns"], [])
        self.assertNotIn("error", responses[0])

    def test_answers_match_local_execution(self):
        # Self-contained: this test seeds its own table so it cannot depend on test order.
        client(self.host, self.port, ["CREATE TABLE IF NOT EXISTS parity (a INTEGER, b TEXT)",
                                      "DELETE FROM parity",
                                      "INSERT INTO parity VALUES (1,'x'),(2,'y'),(3,NULL)"])
        statements = ["SELECT count(*) FROM parity", "SELECT a, b FROM parity ORDER BY a",
                      "SELECT sum(a), avg(a) FROM parity"]
        remote = client(self.host, self.port, statements)
        local = Database()
        local.run("CREATE TABLE parity (a INTEGER, b TEXT)")
        local.run("INSERT INTO parity VALUES (1,'x'),(2,'y'),(3,NULL)")
        for statement, response in zip(statements, remote):
            columns, rows = local.run(statement)
            expected = [[None if value is None else value for value in row] for row in rows]
            self.assertEqual(response["columns"], list(columns))
            self.assertEqual(response["rows"], expected, statement)
        local.close()

    def test_errors_propagate_with_the_engine_wording(self):
        response = client(self.host, self.port, ["SELECT * FROM missing"])[0]
        self.assertEqual(response["error"], "no such table: missing")

    def test_three_clients_agree(self):
        client(self.host, self.port, ["CREATE TABLE IF NOT EXISTS shared (v INTEGER)"])
        client(self.host, self.port, ["DELETE FROM shared", "INSERT INTO shared VALUES (5),(6)"])
        results = []

        def worker():
            results.append(client(self.host, self.port, ["SELECT sum(v) FROM shared"])[0])

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertEqual(result["rows"], [[11]])

    def test_malformed_request_is_answered_not_dropped(self):
        sock = socket.create_connection((self.host, self.port), timeout=5)
        stream = sock.makefile("rwb")
        stream.write(b"not json\n")
        stream.flush()
        response = json.loads(stream.readline().decode())
        self.assertIn("malformed", response["error"])
        sock.close()


if __name__ == "__main__":
    unittest.main()
