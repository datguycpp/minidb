"""The calibration loop's three frozen properties: whitelist, refusal-reason subscription, termination."""

import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "schedule_calibration", os.path.join(HERE, "tools", "schedule_calibration.py"))
calibration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calibration)


class TaxonomyTest(unittest.TestCase):
    def test_an_ambient_refusal_tightens(self):
        self.assertEqual(calibration.classify("span/stability"), "tighten")
        self.assertEqual(calibration.classify("pre-event comparability"), "tighten")

    def test_a_non_ambient_refusal_never_tightens(self):
        for reason in ("dirty tree", "counter invariant", "claim level", "provenance"):
            self.assertEqual(calibration.classify(reason), "never", reason)

    def test_an_unclassified_reason_defaults_to_never(self):
        # Closed by default: the next instrument will invent a reason, and it must not tighten a
        # schedule until the taxonomy is extended by explicit ruling.
        self.assertEqual(calibration.classify("some future instrument said no"), "never")


class CalibrationTest(unittest.TestCase):
    def test_only_ambient_refusals_move_the_tolerance(self):
        trail, final, _ = calibration.calibrate(10.0, 1.0, 6, ["dirty tree", "dirty tree"])
        self.assertEqual(final, 10.0)
        self.assertTrue(all(action.startswith("no change") for _, action, _ in trail))

    def test_the_trail_records_which_hour_failed(self):
        trail, final, _ = calibration.calibrate(10.0, 1.0, 6, ["span/stability", "span/stability"])
        self.assertEqual(final, 8.0)
        self.assertEqual([action for _, action, _ in trail], ["tightened", "tightened"])

    def test_it_declares_unschedulable_instead_of_tightening_forever(self):
        # tolerance * starts_per_cycle < 1 is the operational floor, not an asymptote.
        trail, final, state = calibration.calibrate(2.0, 1.0, 3, ["span/stability"] * 10)
        self.assertEqual(state, "unschedulable")
        self.assertLess(final * 3, 1.0)
        self.assertLess(len(trail), 10, "the loop must stop at the floor, not run the whole trail")

    def test_a_generous_schedule_stays_schedulable(self):
        trail, final, state = calibration.calibrate(10.0, 0.5, 6, ["span/stability"] * 3)
        self.assertEqual(state, "schedulable")
        self.assertEqual(final, 8.5)


if __name__ == "__main__":
    unittest.main()
