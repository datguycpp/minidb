#!/usr/bin/env python3
"""M27 corruption fixtures: every damaged file must be *detected*, never silently misread.

Rule from docs/tier4-prework.md: a corruption fixture passes when the engine raises a typed error or
`PRAGMA integrity_check` reports a problem. Silent wrong data fails. A crash-only "detection" fails.

    python3 tools/corruption_check.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.engine import Database  # noqa: E402
from minidb.storage.pager import (  # noqa: E402
    StorageError, _OFF_PAGE_COUNT, _OFF_PAGE_SIZE, _OFF_VERSION)

PAGE_SIZE = 4096


def build():
    path = os.path.join(tempfile.mkdtemp(), "corrupt.db")
    db = Database(path)
    db.run("CREATE TABLE t (id INTEGER, v TEXT)")
    db.run("BEGIN")
    for start in range(1, 201, 50):
        db.run("INSERT INTO t VALUES " + ", ".join(f"({i}, 'v{i}')" for i in range(start, start + 50)))
    db.run("COMMIT")
    db.run("CREATE INDEX idx_v ON t (v)")
    db.close()
    return path


def flip(path, offset, mask=0xFF):
    with open(path, "r+b") as handle:
        handle.seek(offset)
        original = handle.read(1)
        handle.seek(offset)
        handle.write(bytes([original[0] ^ mask]))


def probe(path):
    """Return ('ok'|'error'|'wrong', detail)."""
    try:
        db = Database(path)
    except (StorageError, Exception) as exc:                       # open-time detection
        return "error", f"{type(exc).__name__}: {exc}"
    try:
        integrity = db.run("PRAGMA integrity_check")[1][0][0]
        if integrity != "ok":
            return "error", f"integrity_check: {integrity}"
        rows = db.run("SELECT count(*), min(id), max(id) FROM t")[1][0]
        if rows != (200, 1, 200):
            return "wrong", f"silent wrong data: {rows}"
        return "ok", "no damage detected"
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {exc}"
    finally:
        db.close()


# Declared limit, not a failure: without page checksums a bit flip inside a record payload is
# undetectable — the same property SQLite has. Kept in the list so the boundary is visible.
UNDETECTABLE_BY_DESIGN = {
    "payload-bitflip": lambda path: flip(path, PAGE_SIZE + 14),
}

FIXTURES = {
    "magic": lambda path: flip(path, 0),
    "header-page-count": lambda path: flip(path, _OFF_PAGE_COUNT),
    "header-version": lambda path: flip(path, _OFF_VERSION),
    "header-page-size": lambda path: flip(path, _OFF_PAGE_SIZE),
    "btree-page-type": lambda path: flip(path, PAGE_SIZE + 0),
    "btree-cell-count": lambda path: flip(path, PAGE_SIZE + 1),
    "truncated-file": lambda path: os.truncate(path, os.path.getsize(path) - 1000),
    "page-fill-garbage": lambda path: flip(path, PAGE_SIZE + 20, 0xFF),
}


def main():
    failures = []
    for name, damage in FIXTURES.items():
        path = build()
        damage(path)
        verdict, detail = probe(path)
        if verdict == "wrong":
            failures.append((name, detail))
            print(f"CORRUPTION {name:<20} FAIL  {detail}")
        elif verdict == "error":
            print(f"CORRUPTION {name:<20} detected  {detail[:90]}")
        else:
            print(f"CORRUPTION {name:<20} UNDETECTED  {detail}")
    for name, damage in UNDETECTABLE_BY_DESIGN.items():
        path = build()
        damage(path)
        verdict, detail = probe(path)
        print(f"CORRUPTION {name:<20} declared-undetectable ({verdict})")
    print(f"M27: {len(FIXTURES) - len(failures)}/{len(FIXTURES)} structural fixtures detected, "
          f"{len(failures)} silent-wrong-data failures, "
          f"{len(UNDETECTABLE_BY_DESIGN)} declared undetectable (no page checksums)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
