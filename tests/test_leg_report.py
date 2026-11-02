"""The reporting step (tools/leg_report.py): gate, comparability, table, absolutes.

The tool's job is to make the reading order mechanical rather than remembered, so the branches
worth pinning are exactly the ones a hurried reader could skip: a file that fails the gate must
produce no table at all, a reference-side move must be named before the ratios are read, and a
workload that lands against its frozen expectation must fail the verdict by name.
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

_spec = importlib.util.spec_from_file_location("leg_report", os.path.join(ROOT, "tools", "leg_report.py"))
leg_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leg_report)

BASE = {"W1": 500.0, "W2": 40.0, "W3": 77.6, "W4": 144.0, "W5": 70.0,
        "W6": 16.2, "W7": 30.0, "W8": 400.0, "W10": 60.0}
OPS = leg_report.OPS


def write_leg(path, ratios=None, sqlite_shift=1.0, drop=(), ratio_shown=None):
    """A leg in the harness's printed shape; each workload carries `rows` equal to its op count."""
    ratios = dict(BASE) if ratios is None else ratios
    lines = ["BENCH first-full-run  module-sqlite 3.53.1  N=7  scale=100000  lookups=1000", ""]
    for name, ratio in ratios.items():
        if name in drop:
            continue
        ops = OPS.get(name, 1)
        sqlite_ms = 10.0 * sqlite_shift
        minidb_ms = sqlite_ms * ratio
        shown = (ratio_shown or {}).get(name, ratio)
        lines += [f"{name}  synthetic {name}          rows={ops}",
                  f"     sqlite_ms median={sqlite_ms:.2f}  raw7=[ {sqlite_ms:.2f} ]",
                  f"     minidb_ms median={minidb_ms:.2f}  raw7=[ {minidb_ms:.2f} ]",
                  f"     ratio={shown:.3f}  output: OK"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def run_tool(self, before, after, extra=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = leg_report.main([before, after, *extra])
        return code, out.getvalue(), err.getvalue()

    def test_a_flat_pair_passes_when_everything_is_expected_equal(self):
        before = write_leg(self.path("b.txt"))
        after = write_leg(self.path("a.txt"))
        expect = "".join(f"{name}=equal," for name in BASE).rstrip(",")
        code, out, _ = self.run_tool(before, after, ("--expect", expect))
        self.assertEqual(code, 0, out)
        self.assertIn("comparability: reference side within", out)
        self.assertIn("per row-visit", out)

    def test_the_write_path_and_c_module_improvements_satisfy_the_defaults(self):
        after_ratios = dict(BASE, W3=0.9, W6=0.27, W7=13.0)
        before = write_leg(self.path("b.txt"))
        after = write_leg(self.path("a.txt"), ratios=after_ratios)
        code, out, _ = self.run_tool(before, after)
        self.assertEqual(code, 0, out)
        self.assertIn("W7", out)
        self.assertIn("write-path uniqueness set", out)
        self.assertIn("verdict: every workload landed as frozen", out)

    def test_a_workload_that_failed_its_expectation_fails_the_verdict_by_name(self):
        before = write_leg(self.path("b.txt"))
        after = write_leg(self.path("a.txt"))            # nothing improved
        code, out, _ = self.run_tool(before, after)
        self.assertEqual(code, 1)
        self.assertIn("W3  wanted improve, got equal", out)
        self.assertIn("unmet expectations that are not regressions", out)

    def test_a_reference_side_move_is_named_before_the_table(self):
        before = write_leg(self.path("b.txt"))
        after = write_leg(self.path("a.txt"), sqlite_shift=1.2)
        expect = "".join(f"{name}=equal," for name in BASE).rstrip(",")
        code, out, _ = self.run_tool(before, after, ("--expect", expect))
        self.assertEqual(code, 0, out)
        self.assertIn("comparability: reference side moved beyond +/-5%", out)
        self.assertLess(out.index("comparability"), out.index("W1 "))

    def test_a_regression_on_a_stable_reference_is_an_engine_finding(self):
        after_ratios = dict(BASE, W5=80.0)              # W5 ratio rises, reference unchanged
        before = write_leg(self.path("b.txt"))
        after = write_leg(self.path("a.txt"), ratios=after_ratios)
        expect = "".join(f"{name}=equal," for name in BASE).rstrip(",")
        code, out, _ = self.run_tool(before, after, ("--expect", expect))
        self.assertEqual(code, 1)
        self.assertIn("regressions -- the parity clause", out)
        self.assertIn("stable reference: engine side", out)
        self.assertNotIn("unmet expectations that are not regressions", out)

    def test_a_regression_whose_reference_moved_is_a_cross_box_reading(self):
        after_ratios = dict(BASE, W5=80.0)
        before = write_leg(self.path("b.txt"))
        after = write_leg(self.path("a.txt"), ratios=after_ratios, sqlite_shift=1.2)
        expect = "".join(f"{name}=equal," for name in BASE).rstrip(",")
        code, out, _ = self.run_tool(before, after, ("--expect", expect))
        self.assertEqual(code, 1)
        self.assertIn("cross-box reading: the reference moved with it", out)

    def test_the_demo_is_labelled_before_it_shows_anything(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = leg_report.main(["--demo"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("DEMO --"), text[:80])
        self.assertIn("nothing here may be quoted as a result", text)
        self.assertLess(text.index("DEMO --"), text.index("W1 "))

    def test_the_demo_reconstructs_the_published_before_leg_within_the_display_quantum(self):
        """Pin target: record-reconstruction from *displayed* medians, not formula consistency.

        The demo multiplies each printed reference median by the printed ratio, so its absolute
        carries the reference's two-decimal display rounding scaled by the ratio -- W1's true 3.47114
        shown as 3.47 leaves ~255 ms on a 779,943 ms workload, W8's 10.0717 as 10.07 leaves ~119 ms.
        The tolerance below is exactly that half-quantum (0.005 ms x ratio / ops), so a future reader
        cannot mistake the display residual for the first arithmetic drift. Pinning against the raw
        medians would fail today by design; that is the quantum's signature, not a bug.
        """
        published = {"W1": 779.94348, "W8": 702.69758}          # this leg's own published figures
        for name, expected in published.items():
            ops = leg_report.OPS[name]
            demo = leg_report.DEMO_SQLITE[name] * leg_report.DEMO_BASE[name] / ops
            quantum = 0.005 * leg_report.DEMO_BASE[name] / ops
            self.assertLessEqual(abs(demo - expected), quantum,
                                 f"{name}: demo {demo:.3f} vs published {expected:.3f}")

    def test_a_leg_that_fails_the_gate_produces_no_table(self):
        before = write_leg(self.path("b.txt"), drop=("W6",))
        after = write_leg(self.path("a.txt"))
        code, out, err = self.run_tool(before, after)
        self.assertEqual(code, 2)
        self.assertNotIn("ratio before", out)
        self.assertIn("FAILS the gate", err)

    def test_an_inconsistent_ratio_is_refused_rather_than_tabulated(self):
        before = write_leg(self.path("b.txt"), ratio_shown={"W5": 1.0})
        after = write_leg(self.path("a.txt"))
        code, _, err = self.run_tool(before, after)
        self.assertEqual(code, 2)
        self.assertIn("FAILS the gate", err)


if __name__ == "__main__":
    unittest.main()
