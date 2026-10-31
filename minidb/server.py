"""MiniDB's network server (milestone 30).

There is no SQLite wire protocol to be compatible with — SQLite is a library, not a server — so the
acceptance is different in kind: N clients get answers identical to local in-process runs, over a
protocol we define. Requests and responses are one JSON object per line.

The server inherits M25's semantics rather than inventing its own: one Database handle, one lock,
so a write from any client is serialised with every other statement, and a client sees the same
snapshot rules a local caller does.

    python3 -m minidb.server --db path/to.db --port 8765
"""

import argparse
import json
import socketserver
import sys
import threading

from .engine import Database, ExecError
from .sql.parser import ParseError


class Session:
    """One Database handle shared by every connection, guarded by one lock.

    Transaction ownership is explicit because the handle is shared: while one client holds an open
    transaction, every other client's statement waits and then fails with the reference wording,
    instead of reading that client's uncommitted rows. After the owner commits, the state is
    visible to everyone. That is the observable form of the isolation the single-writer model
    permits — the alternative (per-client handles) would simply refuse the second connection.
    """

    def __init__(self, path=None):
        self.db = Database(path)
        self.lock = threading.Lock()
        self.owner = None            # connection id holding the open transaction

    def execute(self, sql, client_id=None):
        keyword = sql.strip().split(" ", 1)[0].upper() if sql.strip() else ""
        with self.lock:
            if self.owner is not None and client_id != self.owner:
                return {"error": "database is locked"}
            try:
                result = self.db.run(sql)
            except (ExecError, ParseError) as exc:
                return {"error": str(exc)}
            except Exception as exc:                      # a bug must not kill the connection
                return {"error": f"{type(exc).__name__}: {exc}"}
            if keyword == "BEGIN" and not isinstance(result, dict) and self.db.tx_snapshot is not None:
                self.owner = client_id
            elif keyword in ("COMMIT", "ROLLBACK", "END"):
                self.owner = None
        if not isinstance(result, tuple):
            return {"columns": [], "rows": []}
        columns, rows = result
        return {"columns": list(columns),
                "rows": [[_jsonable(value) for value in row] for row in rows]}

    def release(self, client_id):
        """A client that disappears mid-transaction must not leave the database locked."""
        with self.lock:
            if self.owner == client_id:
                try:
                    self.db.run("ROLLBACK")
                except (ExecError, ParseError):
                    pass
                self.owner = None

    def close(self):
        self.db.close()


def _jsonable(value):
    if isinstance(value, bytes):
        return {"__blob__": value.hex()}
    return value


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self._handle()
        finally:
            self.server.session.release(id(self))

    def _handle(self):
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                response = {"error": "malformed request: expected one JSON object per line"}
            else:
                response = self.server.session.execute(request.get("sql", ""), client_id=id(self))
            self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_close(self):
        super().server_close()
        session = getattr(self, "session", None)
        if session is not None:
            session.close()          # closing the server must close its handle, not leak the file


def serve(path=None, host="127.0.0.1", port=8765):
    session = Session(path)
    server = Server((host, port), Handler)
    server.session = session
    return server


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = serve(args.db, args.host, args.port)
    host, port = server.server_address
    print(f"minidb server listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
