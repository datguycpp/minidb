"""The BLOCK-engine instrument's guards: identity and refusal, not its statistics."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import block_engine_ab  # noqa: E402


class BlockEngineGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_missing_fixture_is_refused(self):
        missing = os.path.join(self.dir, "nope.db")
        self.assertEqual(block_engine_ab.main(["--fixture", missing, "--n", "11"]), 2)

    def test_a_fixture_that_is_not_the_pinned_one_is_refused(self):
        # Identity, not existence: a different 8 MB file with the same name would otherwise be
        # measured and published under the frozen subject's name.
        source = None
        candidates = [os.environ.get("G6_W9_DB"),
                      os.path.expanduser("~/.hermes/profiles/engineer/cache/scratch/m38/w9-fixture.db")]
        source = next((c for c in candidates if c and os.path.exists(c)), None)
        if source is None:
            self.skipTest("no w9 fixture on this machine")
        copy = os.path.join(self.dir, "w9-fixture.db")
        shutil.copyfile(source, copy)
        with open(copy, "r+b") as handle:
            handle.seek(4096 * 5)
            byte = handle.read(1)
            handle.seek(4096 * 5)
            handle.write(bytes([byte[0] ^ 0xFF]))
        self.assertEqual(block_engine_ab.main(["--fixture", copy, "--n", "11"]), 2)


if __name__ == "__main__":
    unittest.main()
