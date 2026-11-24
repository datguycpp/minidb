import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pair_watch  # noqa: E402


def write_log(rows):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for stamp, load in rows:
        handle.write(json.dumps({"t": stamp, "load": load}) + "\n")
    handle.close()
    return handle.name


class PairWatchTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.cleanup = []
        import baseline_attempts
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        handle.close()
        self.cleanup.append(handle.name)
        self._records = (pair_watch.PAIR_RECORD, baseline_attempts.PAIR_RECORD)
        pair_watch.PAIR_RECORD = baseline_attempts.PAIR_RECORD = handle.name

    def tearDown(self):
        import baseline_attempts
        pair_watch.PAIR_RECORD, baseline_attempts.PAIR_RECORD = self._records
        for path in self.cleanup:
            if os.path.exists(path):
                os.unlink(path)

    def ambient(self, rows):
        path = write_log(rows)
        self.cleanup.append(path)
        return path

    def watch_log(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        handle.close()
        self.cleanup.append(handle.name)
        return handle.name

    def test_the_box_lock_is_exclusive_and_self_releasing(self):
        # Two rows' bands overlap, so two watchers can read `yes` on the same stretch; the lock is what
        # keeps one pair from raising the load the other is measured under.
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import baseline_attempts
        path = os.path.join(tempfile.gettempdir(), "minidb-lock-test")
        first = baseline_attempts.acquire_lock(path)
        second = baseline_attempts.acquire_lock(path)
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(second)                       # contended: the second caller is refused
        finally:
            baseline_attempts.release_lock(first)
        third = baseline_attempts.acquire_lock(path)
        self.assertIsNotNone(third)                         # released on close, no cleanup file needed
        baseline_attempts.release_lock(third)
        os.unlink(path)

    def test_a_complete_verdict_stops_the_watcher(self):
        # One miss at 03:00 must not become a pass hunt: the stop gate keys on the LAUNCH row, so a verdict
        # recorded against the captured identity (:0.41) still blocks the pending row (:0.38) that launched it.
        path = self.watch_log()
        with open(path, "w") as handle:
            handle.write(json.dumps({"launch_row": "gate3:d89f689+:0.38",
                                     "live_identity": "gate3:d89f689+:0.41",
                                     "pair_status": "complete", "verdict": "miss"}) + "\n")
        self.assertTrue(pair_watch.verdict_exists("gate3:d89f689+:0.38", path))
        self.assertFalse(pair_watch.verdict_exists("gate3:d89f689+:0.72", path))
        with open(path, "w") as handle:
            handle.write(json.dumps({"launch_row": "gate3:d89f689+:0.38",
                                     "pair_status": "baseline-only"}) + "\n")
        self.assertFalse(pair_watch.verdict_exists("gate3:d89f689+:0.38", path))

    def test_the_watcher_only_spends_on_pending_rows(self):
        with self.assertRaises(SystemExit):
            pair_watch.row_band("gate1:d89f689:2.00")      # live row: not the watcher's business

    def test_a_busy_stretch_records_a_wait_and_spends_nothing(self):
        log = self.watch_log()
        quiet = self.ambient([(self.now - 300 + i * 5, 1.60) for i in range(60)])
        original = pair_watch.AMBIENT
        pair_watch.AMBIENT = quiet
        try:
            base, tol = pair_watch.row_band("gate3:d89f689+:0.38")
            self.assertEqual(pair_watch.poll("gate3:d89f689+:0.38", base, tol, dry_run=True,
                                             watch_log=log), "wait")
        finally:
            pair_watch.AMBIENT = original
        with open(log) as handle:
            self.assertEqual(json.loads(handle.read().strip())["action"], "wait")

    def test_a_complete_pair_without_a_verdict_does_not_stop_the_watcher(self):
        path = self.watch_log()
        with open(path, "w") as handle:
            handle.write(json.dumps({"launch_row": "gate3:d89f689+:0.38", "pair_status": "complete",
                                     "verdict": "undecided"}) + "\n")
        self.assertFalse(pair_watch.verdict_exists("gate3:d89f689+:0.38", path))

    def test_a_schedulable_stretch_would_launch_in_dry_run_and_launch_nothing(self):
        log = self.watch_log()
        path = self.ambient([(self.now - 300 + i * 5, 0.35) for i in range(60)])
        original = pair_watch.AMBIENT
        pair_watch.AMBIENT = path
        try:
            base, tol = pair_watch.row_band("gate3:d89f689+:0.38")
            self.assertEqual(pair_watch.poll("gate3:d89f689+:0.38", base, tol, dry_run=True,
                                             watch_log=log), "spent")
        finally:
            pair_watch.AMBIENT = original
        with open(log) as handle:
            entry = json.loads(handle.read().strip())
        self.assertEqual(entry["action"], "would-launch")
        self.assertEqual(entry["state"], "yes")
        # every poll line names the code that decided, so path-equality never has to stand in for
        # version-equality on a long-lived process
        self.assertIn("rev", entry)


if __name__ == "__main__":
    unittest.main()
