"""The tiny fixture is present, reproducible, and identical across the two revisions it must span."""

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tests", "skeptic"))

import make_tiny_fixture  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINNED = os.path.join(REPO, "tests", "skeptic", "fixtures", "g6-tiny.db")
EXPECTED = (12288, "64a9d5417b7ec44389ffbde5b7e6da52ec53aff157156fbfd3e3caed3a4d4c30")


def identity(path):
    with open(path, "rb") as handle:
        data = handle.read()
    return len(data), hashlib.sha256(data).hexdigest()


class TinyFixtureTest(unittest.TestCase):
    def test_the_committed_bytes_are_the_pinned_ones(self):
        self.assertEqual(identity(PINNED), EXPECTED)

    def test_the_builder_reproduces_the_pinned_bytes(self):
        # Determinism is the audit path, not the provenance: the committed file is what gets
        # measured, and this proves any seat can regenerate it byte-for-byte.
        with tempfile.TemporaryDirectory() as tmp:
            built = make_tiny_fixture.build(os.path.join(tmp, "g6-tiny.db"))
            self.assertEqual(identity(built), EXPECTED)

    def test_the_fixture_answers_the_gate_3_statements(self):
        from minidb.engine import Database
        db = Database(PINNED)
        try:
            for sql in ("SELECT count(*) FROM t WHERE id > 0",
                        "SELECT count(*) FROM t WHERE id > 3",
                        "SELECT count(*) FROM t WHERE id > 4"):
                db.run(sql)
            self.assertEqual(db.run("SELECT count(*) FROM t WHERE id > 0")[1], [(4,)])
            self.assertEqual(db.run("SELECT count(*) FROM t WHERE id > 3")[1], [(1,)])
            self.assertEqual(db.run("SELECT count(*) FROM t WHERE id > 4")[1], [(0,)])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
