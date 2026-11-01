"""The pairing step's own gates (tools/bench_pair.py), exercised on fixtures rather than a run.

The tool exists because a benchmark process can exit 0 with an artifact that exists and contains
nothing; the 100k pair is the run that will use it, so the positive path cannot wait for that run
to be tested. Nine synthetic workloads are enough to pin every branch: completeness, self-
consistency, answer mismatch, and the table itself, including the reference-delta column that
makes a denominator-only move visible.
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location("bench_pair", os.path.join(ROOT, "tools", "bench_pair.py"))
bench_pair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench_pair)

RATIOS = {"W1": 50479.0, "W2": 900.0, "W3": 77.6, "W4": 144.2, "W5": 70.0,
          "W6": 16.2, "W7": 30.0, "W8": 40.0, "W10": 60.0}


def write_run(path, minidb_shift=1.0, sqlite_shift=1.0, output="OK", drop=(), ratio_override=None):
    """A file in the harness's printed shape: header, then per-workload blocks.

    The two sides move independently so a denominator-only move can be modelled: that is the
    artefact the reference-delta column exists to expose.
    """
    lines = ["BENCH first-full-run  module-sqlite 3.53.1  N=7  scale=20000  lookups=1000", ""]
    for name, ratio in RATIOS.items():
        if name in drop:
            continue
        base = 0.02 if name == "W1" else 10.0
        sqlite_ms = base * sqlite_shift
        minidb_ms = base * ratio * minidb_shift
        shown_ratio = (ratio_override or {}).get(name, minidb_ms / sqlite_ms)
        lines += [f"{name}  synthetic {name}          rows=1",
                  f"     sqlite_ms median={sqlite_ms:.2f}  raw7=[ {sqlite_ms:.2f} ]",
                  f"     minidb_ms median={minidb_ms:.2f}  raw7=[ {minidb_ms:.2f} ]",
                  f"     ratio={shown_ratio:.3f}  output: {output}"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


class BenchPairTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.dir, name)

    def run_pair(self, before, after):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bench_pair.main([before, after])

    def test_a_complete_pair_pairs(self):
        before = write_run(self.path("b.txt"))
        after = write_run(self.path("a.txt"), minidb_shift=0.5)
        self.assertEqual(self.run_pair(before, after), 0)

    def test_a_header_only_file_is_refused(self):
        empty = self.path("empty.txt")
        with open(empty, "w", encoding="utf-8") as handle:
            handle.write("BENCH first-full-run  module-sqlite 3.53.1  N=7  scale=20000  lookups=1000\n")
        self.assertEqual(self.run_pair(empty, write_run(self.path("a.txt"))), 2)

    def test_a_partial_file_names_what_is_missing(self):
        partial = write_run(self.path("p.txt"), drop=("W2", "W7"))
        self.assertEqual(self.run_pair(partial, write_run(self.path("a.txt"))), 2)

    def test_a_ratio_that_contradicts_its_medians_is_refused(self):
        broken = write_run(self.path("broken.txt"), ratio_override={"W3": 12.0})
        self.assertEqual(self.run_pair(broken, write_run(self.path("a.txt"))), 4)

    def test_an_answer_mismatch_is_refused(self):
        mismatched = write_run(self.path("m.txt"), output="MISMATCH")
        self.assertEqual(self.run_pair(mismatched, write_run(self.path("a.txt"))), 3)

    def test_a_denominator_only_move_is_visible(self):
        # MiniDB's own medians are identical; only SQLite's moved, so every ratio halves.
        before = write_run(self.path("b.txt"))
        after = write_run(self.path("a.txt"), sqlite_shift=2.0)
        before_runs, after_runs = bench_pair.parse(before), bench_pair.parse(after)
        self.assertAlmostEqual(before_runs["W3"]["minidb_ms"], after_runs["W3"]["minidb_ms"])
        fold, word = bench_pair.verdict(before_runs["W3"]["ratio"], after_runs["W3"]["ratio"])
        self.assertAlmostEqual(fold, 2.0)
        self.assertEqual(word, "improved")   # the table's job is to show it, the reader's to judge


if __name__ == "__main__":
    unittest.main()
