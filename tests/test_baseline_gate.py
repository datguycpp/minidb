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
        # Tests must never write a production artifact: the pair record is a durable evidence file.
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        handle.close()
        self.paths.append(handle.name)
        self._pair_record = baseline_attempts.PAIR_RECORD
        baseline_attempts.PAIR_RECORD = handle.name

    def tearDown(self):
        baseline_attempts.PAIR_RECORD = self._pair_record
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

    def test_the_trailing_band_follows_the_row_not_a_constant(self):
        # A night row targets 0.38; judged against the 0.72 row's floor the night mode reads `no` by
        # construction, so the row that names the mode would refuse it. The band must come from the row.
        path = self.log([(self.now - 300 + i * 5, 0.35) for i in range(60)])
        self.assertEqual(ambient_map.trailing_state(path)["state"], "no")
        self.assertEqual(ambient_map.trailing_state(path, target=0.38, band=0.30)["state"], "yes")

    def test_the_night_row_is_pending_recurrence_with_its_own_band(self):
        sys.path.insert(0, os.path.join(ROOT, "tests", "skeptic"))
        from comparators import lookup
        entry, problem = lookup("gate3:d89f689+:0.38")
        self.assertEqual(problem[0], "pending")
        self.assertTrue(entry.get("pending_recurrence"))
        self.assertEqual(entry["target_load"], 0.38)
        self.assertLess(entry["target_load"] - entry["launch_tolerance"], 0.42)   # reaches below 0.72's floor

    def test_the_aggregate_rule_is_frozen_before_any_verdict_exists(self):
        # Two modes will produce two conditional verdicts. "One passing pair satisfies gate 3" was chosen
        # with zero verdicts in existence; choosing after two disagreements is the post-hoc rule-picking
        # the freeze order forbids.
        self.assertEqual(baseline_attempts.AGGREGATE_RULE, "SINGLE_PASS")

    def test_a_verdict_is_labelled_by_the_mode_that_spent_it(self):
        path = self.log([(self.now - 300 + i * 5, 0.35) for i in range(60)])
        original = baseline_attempts.AMBIENT
        baseline_attempts.AMBIENT = path
        original_log = baseline_attempts.LOG
        baseline_attempts.LOG = os.devnull
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                baseline_attempts.main(["--simulate", "0", "--no-wait",
                                        "--comparator", "gate3:d89f689+:0.38",
                                        "--subject-comparator", "gate3:d89f689+:0.38"])
        finally:
            baseline_attempts.LOG = original_log
            baseline_attempts.AMBIENT = original
        output = buffer.getvalue()
        self.assertIn("mode=night", output)
        self.assertIn("CONDITIONAL on the night mode", output)

    def test_a_verdict_carries_both_legs_spreads(self):
        # The comparison is median-vs-median at N=7, so the spread is the only robustness information the
        # artifact can hold: a pass at 2% and a pass at 30% are different findings.
        path = self.log([(self.now - 300 + i * 5, 0.35) for i in range(60)])
        original = (baseline_attempts.AMBIENT, baseline_attempts.LOG, baseline_attempts.read_load,
                    baseline_attempts.run_attempt)
        baseline_attempts.AMBIENT, baseline_attempts.LOG = path, os.devnull
        baseline_attempts.read_load = lambda: 0.38
        calls = {"n": 0}

        def fake(number, spec, *extra, **kwargs):
            calls["n"] += 1
            loop = 1030.0 if calls["n"] == 1 else 900.0
            spread = 5.8 if calls["n"] == 1 else 3.1
            return ({"attempt": number, "exit": 0, "seconds": 1.0,
                     "values": {"loop": loop, "loop_spread": spread}}, "")

        baseline_attempts.run_attempt = fake
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = baseline_attempts.main(["--no-wait", "--comparator", "gate3:d89f689+:0.38",
                                               "--subject-comparator", "gate3:d89f689+:0.38"])
        finally:
            (baseline_attempts.AMBIENT, baseline_attempts.LOG, baseline_attempts.read_load,
             baseline_attempts.run_attempt) = original
        output = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("status=miss", output)              # 900 > 1030/2
        self.assertIn("ceiling=515.0", output)
        self.assertIn("spreads=5.8%/3.1%", output)

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
            self.assertEqual(baseline_attempts.main(["--simulate", "0", "--no-wait",
                                                     "--comparator", "gate3:d89f689+:0.38"]), 4)
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
        baseline_attempts.mode_state = lambda *a, **k: states.pop(0) if len(states) > 1 else states[0]
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
        baseline_attempts.mode_state = lambda *a, **k: states.pop(0) if len(states) > 1 else states[0]
        baseline_attempts.LOG = os.devnull
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = baseline_attempts.main(["--simulate", "0", "--no-wait",
                                               "--comparator", "gate3:d89f689+:0.38",
                                               "--subject-comparator", "gate3:d89f689+:0.38"])
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
                                               "--comparator", "gate3:d89f689+:0.38",
                                               "--subject-comparator", "gate3:d89f689+:0.38"])
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
            self.assertEqual(baseline_attempts.main(["--simulate", "0", "--no-wait",
                                                     "--comparator", "gate3:d89f689+:0.38"]), 0)
        finally:
            baseline_attempts.AMBIENT, baseline_attempts.LOG = original_ambient, original_log


if __name__ == "__main__":
    unittest.main()
