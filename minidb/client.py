"""MiniDB's Python binding (milestone 31).

A thin client for the server: connect, run statements, get rows back, and receive server-side
errors as a typed exception whose message is the engine's own wording — identical to what a local
`Database` raises, which is the acceptance the milestone asks for.

    from minidb.client import Client

    with Client("127.0.0.1", 8765) as client:
        client.execute("CREATE TABLE t (a INTEGER)")
        client.execute("INSERT INTO t VALUES (1), (2)")
        columns, rows = client.execute("SELECT sum(a) FROM t")
"""

import json
import socket


class ServerError(Exception):
    """A SQL error reported by the server, carrying the engine's message verbatim."""


class Client:
    def __init__(self, host="127.0.0.1", port=8765, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.stream = self.sock.makefile("rwb")

    def execute(self, sql):
        self.stream.write((json.dumps({"sql": sql}) + "\n").encode("utf-8"))
        self.stream.flush()
        line = self.stream.readline()
        if not line:
            raise ConnectionError("server closed the connection")
        response = json.loads(line.decode("utf-8"))
        if "error" in response:
            raise ServerError(response["error"])
        return response.get("columns", []), [tuple(row) for row in response.get("rows", [])]

    def close(self):
        try:
            self.stream.close()
        finally:
            self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
