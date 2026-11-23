import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "skeptic"))

from comparators import COMPARATORS, lookup  # noqa: E402


class ComparatorTest(unittest.TestCase):
    def test_a_live_identity_resolves_to_its_frozen_entry(self):
        entry, problem = lookup("gate1:d89f689:2.00")
        self.assertIsNone(problem)
        self.assertEqual(entry["ceiling_ms"], 259.0)
        self.assertEqual(entry["base_load"], 2.00)

    def test_the_233_row_is_retired_by_the_quiet_floor_row(self):
        entry, problem = lookup("gate3:d89f689+:2.33")
        self.assertEqual(problem, ("retired", "gate3:d89f689+:0.72"))
        self.assertEqual(entry["ceiling_ms"], 1934.4)   # history stays; only the pointer moved

    def test_a_typoed_load_is_drift_against_the_frozen_identity(self):
        entry, problem = lookup("gate1:d89f689:2.31")
        self.assertEqual(problem, ("drift", "gate1:d89f689:2.00"))
        self.assertEqual(entry["base_load"], 2.00)

    def test_a_superseded_baseline_is_retired_not_unknown(self):
        entry, problem = lookup("gate3:d89f689:2.00")
        self.assertEqual(problem, ("retired", "gate3:d89f689+:2.33"))
        self.assertEqual(entry["ceiling_ms"], 1952.7)

    def test_an_unfrozen_identity_is_unknown(self):
        entry, problem = lookup("gate9:abc:1.00")
        self.assertEqual(problem, ("unknown", None))
        self.assertIsNone(entry)

    def test_every_retired_entry_names_a_chain_ending_live_or_pending(self):
        # Retirement is transitive: 2.00 -> 2.33 -> 0.72, where the last is pre-registered and still
        # awaiting its capture. The test follows the chain so a row cannot point at another dead end.
        for identity, entry in COMPARATORS.items():
            if not entry.get("retired"):
                continue
            current, seen = entry["retired"], set()
            while current and current not in seen:
                seen.add(current)
                successor, problem = lookup(current)
                self.assertIsNotNone(successor, "%s -> %s is not a row" % (identity, current))
                if problem is None or problem[0] == "pending":
                    break
                current = successor.get("retired")
            else:
                self.fail("%s's retirement chain does not terminate live or pending" % identity)

    def test_a_pending_row_refuses_with_its_own_reason(self):
        for identity, entry in COMPARATORS.items():
            if entry.get("pending"):
                _, problem = lookup(identity)
                self.assertEqual(problem[0], "pending", identity)
                self.assertIsNone(entry["base_load"])
                self.assertIsNone(entry["ceiling_ms"])

    def test_a_pending_row_carries_the_target_and_the_launch_tolerance(self):
        pending = [entry for entry in COMPARATORS.values() if entry.get("pending")]
        self.assertTrue(pending, "expected at least one pre-registered row")
        for entry in pending:
            self.assertIsNotNone(entry["target_load"])
            self.assertIsNone(entry["captured_pre_load"])


if __name__ == "__main__":
    unittest.main()
