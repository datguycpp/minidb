import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import ambient_map  # noqa: E402


def write_log(rows):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for stamp, load in rows:
        handle.write(json.dumps({"t": stamp, "load": load}) + "\n")
    handle.close()
    return handle.name


class AmbientModesTest(unittest.TestCase):
    def test_a_quiet_hour_is_schedulable_and_a_busy_hour_is_not(self):
        now = time.time()
        quiet = [(now - 3 * 3600 + i, 0.55) for i in range(0, 3600, 60)]
        busy = [(now - 3600 + i, 1.60) for i in range(0, 3600, 60)]
        path = write_log(quiet + busy)
        try:
            self.assertEqual(ambient_map.modes(path, max_age=10 ** 9), 0)
        finally:
            os.unlink(path)

    def test_a_stale_log_is_refused_not_scheduled(self):
        path = write_log([(time.time() - 10 ** 6, 0.55)])
        try:
            self.assertEqual(ambient_map.modes(path, max_age=120), 3)
        finally:
            os.unlink(path)

    def test_a_thin_hour_reads_insufficient_rather_than_a_share(self):
        # 12 samples (one minute at 5 s) carries a ~+/-29 pp CI at a 50% threshold: the state is unknown,
        # not "no". The guard is what keeps the launch rule from acting on noise.
        now = time.time()
        thin = [(now - 300 + i * 5, 0.55) for i in range(12)]
        path = write_log(thin)
        try:
            self.assertEqual(ambient_map.modes(path, max_age=10 ** 9), 0)
        finally:
            os.unlink(path)
        self.assertEqual(ambient_map.MODE_MIN_SAMPLES, 60)

    def test_mode_share_is_definitional(self):
        # "majority in-gate" is the definition, not a tuned cutoff: exactly half is schedulable.
        self.assertEqual(ambient_map.MODE_SHARE, 0.50)
        self.assertEqual(ambient_map.TARGET_LOAD, 0.72)
        self.assertEqual(ambient_map.BAND, 0.30)


if __name__ == "__main__":
    unittest.main()
