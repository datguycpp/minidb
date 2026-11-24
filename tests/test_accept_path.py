import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE = os.path.join(ROOT, "tests", "skeptic", "baseline_g6_quiet.py")


class AcceptPathTest(unittest.TestCase):
    """The incident's permanent test: the REAL subprocess against a PENDING row.

    Every harness test before this one patched readings and simulated exits, so the only path production
    runs -- the real capture subprocess against a real registry -- had zero coverage, and 4,497 empty
    attempt records accumulated behind a green suite. A simulated exit proves control flow; this runs the
    binary that refused.
    """

    def run_capture(self, *args):
        return subprocess.run([sys.executable, CAPTURE, "--series", "LOOP", *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_a_pending_row_without_a_sidecar_is_refused_with_exit_6(self):
        result = self.run_capture("--comparator", "gate3:d89f689+:0.38")
        self.assertEqual(result.returncode, 6)          # instrument refusal, distinct from 3/4
        self.assertIn("is PENDING", result.stderr)

    def test_a_pending_row_with_a_sidecar_gets_past_the_pending_wall(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"identity": "gate3:d89f689+:0.38", "base_load": 0.38, "gate": "gate3"}, handle)
        handle.close()
        try:
            result = self.run_capture("--comparator", "gate3:d89f689+:0.38",
                                      "--measured-row", handle.name)
        finally:
            os.unlink(handle.name)
        # It may still refuse on the box's load -- that is admissibility, not the pending wall. What must
        # never happen again is `is PENDING` with exit 6 on the harness's own path.
        self.assertNotEqual(result.returncode, 6)
        self.assertIn("measured-row sidecar=", result.stderr + result.stdout)
        self.assertNotIn("is PENDING", result.stderr)


class WatcherStopTest(unittest.TestCase):
    def test_the_watcher_halts_once_on_an_instrument_refusal(self):
        directory = tempfile.mkdtemp()
        ambient = os.path.join(directory, "amb.jsonl")
        with open(ambient, "w") as handle:
            for index in range(60):
                handle.write(json.dumps({"t": time.time() - 300 + index * 5, "load": 0.35}) + "\n")
        fake = os.path.join(directory, "fake_harness.py")
        with open(fake, "w") as handle:
            handle.write("import sys\nsys.exit(6)\n")
        watch = os.path.join(directory, "watch.jsonl")
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import pair_watch as pw\n"
            "pw.AMBIENT = %r\n"
            "pw.HARNESS = %r\n"
            "sys.exit(pw.main(['--comparator', 'gate3:d89f689+:0.38', '--interval', '1', '--log', %r]))\n"
            % (os.path.join(ROOT, "tools"), ambient, fake, watch)
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env=dict(os.environ, PYTHONPATH=os.path.join(ROOT, "tools")))
        with open(watch) as handle:
            actions = [json.loads(line)["action"] for line in handle if line.strip()]
        self.assertEqual(result.returncode, 0)          # clean stop, so on-failure will not restart
        self.assertEqual(actions.count("launch"), 1)    # exactly one launch, not a loop
        self.assertIn("stop", actions)


if __name__ == "__main__":
    unittest.main()
