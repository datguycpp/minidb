"""Build the gate-3 LOOP fixture deterministically.

The pinned bytes are built by the revision the gate-3 baseline runs (the pre-change revision at
`d89f689`), and a HEAD build must reproduce the same `(bytes, sha256)` tuple. That equality is the
write-path check QA asked for: if the newer engine writes even one byte differently, the divergence
is a finding about the write path, recorded before any LOOP number exists.

Shape, chosen for the statements the gate runs (`SELECT count(*) FROM t WHERE id > i`, i in
0..9999): one table `t (id INTEGER PRIMARY KEY, v TEXT)` with ROWS rows inserted in a single
transaction, values `v<id>` — deterministic, no clock, no randomness, no autoincrement holes.

Usage: python tests/skeptic/make_tiny_fixture.py --out /tmp/g6-tiny.db
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from minidb.engine import Database  # noqa: E402

ROWS = 4


def build(path):
    if os.path.exists(path):
        os.remove(path)
    db = Database(path)
    try:
        db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.run("BEGIN")
        for i in range(1, ROWS + 1):
            db.run(f"INSERT INTO t VALUES ({i}, 'v{i}')")
        db.run("COMMIT")
    finally:
        db.close()
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="/tmp/g6-tiny.db")
    args = parser.parse_args()
    print(build(args.out))
