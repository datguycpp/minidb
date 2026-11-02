"""The machine-state duty: a leg records the box's load beside every workload.

The rule exists because the record could not answer "was the box quiet?" from its own artifacts —
the 100k pair's references each moved +26% under a standing loadavg of ~3.4, and that had to be
recovered from `journalctl` a day later. A leg that records the number beside the workload answers
it at read time instead. Older legs never wrote the field, so parsing it is optional and their files
must keep parsing unchanged.
"""

import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

_spec = importlib.util.spec_from_file_location("bench_pair", os.path.join(ROOT, "tools", "bench_pair.py"))
bench_pair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench_pair)

_bspec = importlib.util.spec_from_file_location("benchmark", os.path.join(ROOT, "tools", "benchmark.py"))
benchmark = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(benchmark)


class LoadavgTest(unittest.TestCase):
    def test_the_helper_reads_a_plausible_number(self):
        load = benchmark.loadavg()
        self.assertTrue(load is None or load >= 0.0)
        if load is not None:
            self.assertLess(load, 1000.0)

    def test_a_leg_with_the_field_parses_it(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "leg.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "BENCH first-full-run  module-sqlite 3.53.1  N=7  scale=100000  lookups=1000\n"
                    "\n"
                    "W3  full-scan aggregate                      rows=1  loadavg=3.41\n"
                    "     sqlite_ms median=9.02  raw7=[ 9.02 ]\n"
                    "     minidb_ms median=8.37  raw7=[ 8.37 ]\n"
                    "     ratio=0.928  output: OK\n")
            runs = bench_pair.parse(path)
        self.assertEqual(runs["W3"]["rows"], 1)
        self.assertEqual(runs["W3"]["loadavg"], 3.41)
        self.assertEqual(runs["W3"]["title"], "full-scan aggregate")

    def test_a_leg_without_the_field_parses_unchanged(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "leg.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "BENCH first-full-run  module-sqlite 3.53.1  N=7  scale=100000  lookups=1000\n"
                    "\n"
                    "W7  bulk write 10k single-row commits        rows=1\n"
                    "     sqlite_ms median=22.33  raw7=[ 22.33 ]\n"
                    "     minidb_ms median=1330.00  raw7=[ 1330.00 ]\n"
                    "     ratio=59.549  output: OK\n")
            runs = bench_pair.parse(path)
        self.assertEqual(runs["W7"]["title"], "bulk write 10k single-row commits")
        self.assertNotIn("loadavg", runs["W7"])


if __name__ == "__main__":
    unittest.main()
