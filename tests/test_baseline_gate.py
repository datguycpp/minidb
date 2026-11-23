import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ambient_map  # noqa: E402
import baseline_attempts  # noqa: E402


def write_log(rows):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for stamp, load in rows:
        handle.write(json.dumps({"t": stamp, "load": load}) + "\n")
    handle.close()
    return handle.name


class TrailingStateTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.paths = []

    def tearDown(self):
        for path in self.paths:
            if os.path.exists(path):
                os.unlink(path)

    def log(self, rows):
        path = write_log(rows)
        self.paths.append(path)
        return path

    def test_a_quiet_stretch_is_schedulable(self):
        path = self.log([(self.now - 300 + i * 5, 0.55) for i in range(60)])
        self.assertEqual(ambient_map.trailing_state(path)["state"], "yes")

    def test_a_busy_stretch_is_not(self):
        path = self.log([(self.now - 300 + i * 5, 1.60) for i in range(60)])
        self.assertEqual(ambient_map.trailing_state(path)["state"], "no")

    def test_too_few_samples_is_insufficient_not_a_no(self):
        path = self.log([(self.now - 60 + i * 5, 0.55) for i in range(12)])
        self.assertEqual(ambient_map.trailing_state(path)["state"], "insufficient")

    def test_a_stale_log_is_stale(self):
        path = self.log([(self.now - 10 ** 5, 0.55)])
        self.assertEqual(ambient_map.trailing_state(path)["state"], "stale")

    def test_the_harness_refuses_to_wait_in_an_unschedulable_mode(self):
        # The two-gate rule is enforced in the tool, not only in the ruling: a busy stretch exits 4 before
        # any attempt is spent, even though the pointwise gate would admit any single dip inside it.
        path = self.log([(self.now - 300 + i * 5, 1.60) for i in range(60)])
        original = baseline_attempts.AMBIENT
        baseline_attempts.AMBIENT = path
        try:
            self.assertEqual(baseline_attempts.main(["--simulate", "0", "--no-wait"]), 4)
        finally:
            baseline_attempts.AMBIENT = original

    def test_a_mode_flip_during_the_wait_aborts_the_attempt(self):
        # Precondition passes, then the stretch flips while the harness waits: the second evaluation is the
        # one that guards the spend, so the attempt is aborted rather than launched on a stale `yes`.
        path = self.log([(self.now - 300 + i * 5, 0.55) for i in range(60)])
        original = baseline_attempts.AMBIENT
        baseline_attempts.AMBIENT = path
        states = [{"state": "yes", "share": 1.0, "n": 60, "age": 5.0},
                  {"state": "no", "share": 0.2, "n": 60, "age": 5.0}]
        original_gate = baseline_attempts.mode_state
        baseline_attempts.mode_state = lambda: states.pop(0)
        try:
            self.assertEqual(baseline_attempts.main(["--simulate", "0", "--no-wait",
                                                     "--log", os.devnull]), 3)
        finally:
            baseline_attempts.mode_state = original_gate
            baseline_attempts.AMBIENT = original

    def test_an_aborted_subject_leg_leaves_a_valid_baseline(self):
        # The pair is two spends. If the stretch ends between the legs, the baseline is NOT void: it is a
        # baseline-only half-pair, the ceiling still derives from it, and the subject follows in the next
        # matched stretch with the gap printed.
        path = self.log([(self.now - 300 + i * 5, 0.55) for i in range(60)])
        original = baseline_attempts.AMBIENT
        baseline_attempts.AMBIENT = path
        states = [{"state": "yes", "share": 1.0, "n": 60, "age": 5.0},
                  {"state": "yes", "share": 1.0, "n": 60, "age": 5.0},
                  {"state": "no", "share": 0.2, "n": 60, "age": 5.0}]
        original_gate, original_log = baseline_attempts.mode_state, baseline_attempts.LOG
        baseline_attempts.mode_state = lambda: states.pop(0)
        baseline_attempts.LOG = os.devnull
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = baseline_attempts.main(["--simulate", "0", "--no-wait",
                                               "--subject-comparator", "gate1:d89f689:2.00"])
            self.assertEqual(code, 0)                       # a baseline was obtained
            self.assertIn("pair=baseline-only", buffer.getvalue())
        finally:
            baseline_attempts.mode_state, baseline_attempts.LOG = original_gate, original_log
            baseline_attempts.AMBIENT = original

    def test_a_subject_leg_on_a_quiet_stretch_completes_the_pair(self):
        path = self.log([(self.now - 300 + i * 5, 0.55) for i in range(60)])
        original = baseline_attempts.AMBIENT
        baseline_attempts.AMBIENT = path
        original_log = baseline_attempts.LOG
        baseline_attempts.LOG = os.devnull
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = baseline_attempts.main(["--simulate", "0", "--no-wait",
                                               "--subject-comparator", "gate1:d89f689:2.00"])
            self.assertEqual(code, 0)
            self.assertIn("pair=complete", buffer.getvalue())
        finally:
            baseline_attempts.LOG = original_log
            baseline_attempts.AMBIENT = original

    def test_the_harness_proceeds_in_a_schedulable_mode(self):
        path = self.log([(self.now - 300 + i * 5, 0.55) for i in range(60)])
        original_ambient, original_log = baseline_attempts.AMBIENT, baseline_attempts.LOG
        baseline_attempts.AMBIENT, baseline_attempts.LOG = path, os.devnull
        try:
            self.assertEqual(baseline_attempts.main(["--simulate", "0", "--no-wait"]), 0)
        finally:
            baseline_attempts.AMBIENT, baseline_attempts.LOG = original_ambient, original_log


if __name__ == "__main__":
    unittest.main()
