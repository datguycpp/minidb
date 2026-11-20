"""The gate-3 decomposition instrument's verdict rule and its interval."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import gate3_split  # noqa: E402


class ReachabilityRuleTest(unittest.TestCase):
    ceiling = 1934.4

    def test_interval_wholly_above_the_ceiling_means_parser_only_cannot_pass(self):
        verdict = gate3_split.classify(2289.9, 2500.9, self.ceiling)
        self.assertIn("PARSER-ONLY CANNOT PASS", verdict)

    def test_interval_wholly_below_the_ceiling_means_parser_work_could_pass(self):
        verdict = gate3_split.classify(1200.0, 1500.0, self.ceiling)
        self.assertIn("COULD PASS", verdict)

    def test_an_interval_spanning_the_ceiling_establishes_nothing(self):
        # The point estimate may clear the bar; the rule reads the interval, so it must say so.
        verdict = gate3_split.classify(1800.0, 2100.0, self.ceiling)
        self.assertIn("NOT ESTABLISHED", verdict)

    def test_the_bootstrap_is_deterministic(self):
        values = [2063.9, 2295.5, 2325.8, 2488.8, 2500.9, 2363.0, 2124.3, 2289.9, 2516.1,
                  2714.0, 2474.6]
        self.assertEqual(gate3_split.bootstrap_ci(values), gate3_split.bootstrap_ci(values))


if __name__ == "__main__":
    unittest.main()
