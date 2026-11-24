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


class UnexpectedExitTest(unittest.TestCase):
    """Fail-closed: an exit the watcher does not recognise is a new state or a crash, never a window."""

    def run_with_exit(self, code, once=False):
        directory = tempfile.mkdtemp()
        ambient = os.path.join(directory, "amb.jsonl")
        with open(ambient, "w") as handle:
            for index in range(60):
                handle.write(json.dumps({"t": time.time() - 300 + index * 5, "load": 0.35}) + "\n")
        fake = os.path.join(directory, "fake_harness.py")
        with open(fake, "w") as handle:
            handle.write("import sys\nsys.exit(%d)\n" % code)
        watch = os.path.join(directory, "watch.jsonl")
        snippet = (
            "import sys\nsys.path.insert(0, %r)\nimport pair_watch as pw\n"
            "pw.AMBIENT = %r\npw.HARNESS = %r\n"
            "sys.exit(pw.main(['--comparator', 'gate3:d89f689+:0.38', '--interval', '1', '--log', %r%s]))\n"
            % (os.path.join(ROOT, "tools"), ambient, fake, watch, ", '--once'" if once else "")
        )
        result = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True,
                                timeout=30, env=dict(os.environ, PYTHONPATH=os.path.join(ROOT, "tools")))
        with open(watch) as handle:
            actions = [json.loads(line)["action"] for line in handle if line.strip()]
        return result, actions

    def test_a_crash_exit_stops_instead_of_looping(self):
        result, actions = self.run_with_exit(2)
        self.assertEqual(result.returncode, 0)                 # clean stop; on-failure will not restart
        self.assertEqual(actions.count("launch"), 1)
        self.assertIn("stop", actions)

    def test_known_retryable_exits_still_wait(self):
        for code in (1, 3, 4):
            result, actions = self.run_with_exit(code, once=True)
            self.assertEqual(actions, ["launch", "invoked"])   # no stop: the box may change
            self.assertEqual(result.returncode, 3)


class ForwardCountTest(unittest.TestCase):
    def test_one_invocation_is_counted_once_and_an_unanswered_launch_is_named(self):
        # `launch` and `launched` are two lines for ONE invocation; summing them doubled the count to
        # 2,390/775 when the true figure was 1,195/388.
        directory = tempfile.mkdtemp()
        watch = os.path.join(directory, "watch.jsonl")
        attempts = os.path.join(directory, "attempts.jsonl")
        with open(watch, "w") as handle:
            handle.write(json.dumps({"row": "gate3:d89f689+:0.38", "action": "launch"}) + "\n")
            handle.write(json.dumps({"row": "gate3:d89f689+:0.38", "action": "invoked", "exit": 1}) + "\n")
            handle.write(json.dumps({"row": "gate3:d89f689+:0.38", "action": "launch"}) + "\n")
        open(attempts, "w").close()
        import sys as _sys
        _sys.path.insert(0, os.path.join(ROOT, "tools"))
        import ambient_map
        forwards, measured, returned = ambient_map.forward_counts(watch, attempts)
        self.assertEqual(forwards, {"gate3:d89f689+:0.38": 2})   # two invocations, not three lines
        self.assertEqual(measured, 0)
        self.assertEqual(returned, 1)                            # one launch has no return line: in flight


if __name__ == "__main__":
    unittest.main()
