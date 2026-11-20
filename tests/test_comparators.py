import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "skeptic"))

from comparators import COMPARATORS, lookup  # noqa: E402


class ComparatorTest(unittest.TestCase):
    def test_a_live_identity_resolves_to_its_frozen_entry(self):
        entry, problem = lookup("gate3:d89f689+:2.33")
        self.assertIsNone(problem)
        self.assertEqual(entry["ceiling_ms"], 1934.4)
        self.assertEqual(entry["base_load"], 2.33)

    def test_a_typoed_load_is_drift_against_the_frozen_identity(self):
        entry, problem = lookup("gate3:d89f689+:2.31")
        self.assertEqual(problem, ("drift", "gate3:d89f689+:2.33"))
        self.assertEqual(entry["base_load"], 2.33)

    def test_a_superseded_baseline_is_retired_not_unknown(self):
        entry, problem = lookup("gate3:d89f689:2.00")
        self.assertEqual(problem, ("retired", "gate3:d89f689+:2.33"))
        self.assertEqual(entry["ceiling_ms"], 1952.7)

    def test_an_unfrozen_identity_is_unknown(self):
        entry, problem = lookup("gate9:abc:1.00")
        self.assertEqual(problem, ("unknown", None))
        self.assertIsNone(entry)

    def test_every_retired_entry_names_a_live_successor(self):
        for identity, entry in COMPARATORS.items():
            if entry.get("retired"):
                successor, problem = lookup(entry["retired"])
                self.assertIsNone(problem, "%s names %s, which is not live"
                                  % (identity, entry["retired"]))
                self.assertIsNotNone(successor)


if __name__ == "__main__":
    unittest.main()
