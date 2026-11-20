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


class ProbeTest(unittest.TestCase):
    def test_the_probe_attributes_the_aggregate_path_and_none_is_a_no_op(self):
        import tempfile
        from minidb.engine import Database

        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/p.db"
            db = Database(path)
            try:
                db.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
                for i in range(1, 6):
                    db.run("INSERT INTO t VALUES (%d, 'v%d')" % (i, i))
                probe = {"prep_ns": 0, "scan_ns": 0, "filter_ns": 0, "complex_ns": 0,
                         "run_parse_ns": 0, "run_exec_ns": 0, "exec_body_ns": 0,
                         "aggregate": 0, "simple_ns": 0, "simple_hits": 0, "simple_misses": 0}
                db.plan_probe = probe
                self.assertEqual(db.run("SELECT count(*) FROM t WHERE id > 1")[1], [(4,)])
                self.assertEqual(probe["aggregate"], 1)
                self.assertGreater(probe["prep_ns"], 0)
                self.assertGreater(probe["scan_ns"], 0)
                # The row filter is timed apart from preparation and stays inside the path's total.
                self.assertGreater(probe["filter_ns"], 0)
                self.assertGreater(probe["complex_ns"],
                                   probe["prep_ns"] + probe["scan_ns"] + probe["filter_ns"])
                # run() is split where the frozen subject spends it: parse lookup, execute wrapper,
                # and the body -- which contains the complex path it is measured against.
                self.assertGreater(probe["run_parse_ns"], 0)
                self.assertGreater(probe["run_exec_ns"], 0)
                self.assertGreater(probe["exec_body_ns"], probe["complex_ns"])
                db.plan_probe = None
                self.assertEqual(db.run("SELECT count(*) FROM t WHERE id > 1")[1], [(4,)])
                self.assertEqual(probe["aggregate"], 1)
            finally:
                db.close()


class CacheCounterTest(unittest.TestCase):
    def test_a_cleared_parse_leg_counts_its_totals_not_its_delta(self):
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "tools"))
        import gate3_split as rig
        from collections import namedtuple
        Info = namedtuple("Info", "hits misses maxsize currsize")
        legs = {"loop": [0, 0], "parse": [0, 0]}
        cleared = []
        # Two back-to-back parse legs: before == after == 10k on the second, and a delta would read
        # zero. The totals rule keeps the leg.
        rig._count_cache_leg("parse", Info(0, 10000, 1024, 1024), Info(0, 10000, 1024, 1024),
                             legs, cleared)
        rig._count_cache_leg("parse", Info(0, 20000, 1024, 1024), Info(0, 10000, 1024, 1024),
                             legs, cleared)
        self.assertEqual(legs["parse"], [0, 20000])
        self.assertEqual(cleared, [])

    def test_a_loop_leg_that_clears_is_recorded_as_a_violation(self):
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "tools"))
        import gate3_split as rig
        from collections import namedtuple
        Info = namedtuple("Info", "hits misses maxsize currsize")
        legs = {"loop": [0, 0], "parse": [0, 0]}
        cleared = []
        rig._count_cache_leg("loop", Info(0, 20000, 1024, 1024), Info(0, 10000, 1024, 1024),
                             legs, cleared)
        self.assertEqual(cleared, ["loop"])
