import contextlib
import io
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

    def test_the_valley_is_read_off_hourly_medians_not_day_medians(self):
        # One day can hold two states (0.35 and 1.62): its median describes neither, so a day-level spread
        # cannot locate the empty region. The hourly medians can, and the widest adjacent gap is the valley.
        now = time.time()
        first = [(now - 7200 + i * 5, 0.35) for i in range(720)]
        second = [(now - 3600 + i * 5, 1.62) for i in range(720)]
        path = write_log(first + second)
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                ambient_map.modes(path, max_age=10 ** 9)
        finally:
            os.unlink(path)
        output = buffer.getvalue()
        self.assertIn("valley: no hour's median lies between 0.35 and 1.62", output)
        self.assertIn("band split:", output)

    def test_the_horizon_verdict_depends_on_liveness(self):
        # A dead sampler must never launder itself into a deadline: expired + live is a box finding,
        # expired + stale is "insufficient observation", and a frozen file cannot earn the first.
        now = time.time()
        live = write_log([(now - 600 + i * 5, 0.5) for i in range(120)])
        stale = write_log([(now - 10 ** 6, 0.5)])
        past = (2026, 11, 1, 0, 0, 0)
        try:
            empty = write_log([])
            fresh = ambient_map.horizon_status(live, end=past, watch_log=empty, attempt_log=empty)
            self.assertTrue(fresh["expired"])
            self.assertIn("no schedulable window observed", fresh["outcome"])
            dead = ambient_map.horizon_status(stale, end=past, watch_log=empty, attempt_log=empty)
            self.assertIn("insufficient observation", dead["outcome"])
            future = ambient_map.horizon_status(live)
            self.assertFalse(future["expired"])
            self.assertIsNone(future["outcome"])
        finally:
            os.unlink(live)
            os.unlink(stale)
            os.unlink(empty)

    def test_coverage_is_sampled_time_not_span_minus_gaps(self):
        # Consecutive gaps partition the span, so `span - sum(gaps)` is identically zero: a metric that
        # would have reported a 56-hour log with a 45-hour hole as 0 h of coverage.
        now = time.time()
        path = write_log([(now - 7200 + i * 5, 0.5) for i in range(120)]
                         + [(now - 300 + i * 5, 0.5) for i in range(60)])
        try:
            status = ambient_map.horizon_status(path)
            self.assertGreater(status["coverage_h"], 0.1)
            self.assertGreater(status["largest_gap_h"], 1.0)
        finally:
            os.unlink(path)

    def test_row_outcomes_are_replayed_per_row(self):
        # The expiry verdict must say WHICH quiet failed to return. The replay answers from the log, so a
        # row's "never reached yes" is a measurement rather than a memory of the evenings.
        now = time.time()
        night = [(now - 4 * 3600 + i * 5, 0.30) for i in range(720)]
        busy = [(now - 3600 + i * 5, 1.60) for i in range(720)]
        path = write_log(night + busy)
        try:
            rows = ambient_map.row_outcomes(path)
            labels = {row["label"]: row for row in rows}
            self.assertIn("night", labels)
            self.assertGreaterEqual(labels["night"]["yes_runs"], 1)
            self.assertEqual(labels["afternoon"]["yes_runs"], 0)   # busy hour: never reached yes
        finally:
            os.unlink(path)

    def test_the_third_outcome_needs_forwards_and_no_measurement(self):
        # "windows-observed-instrument-unable" is quotable only with its counts: yes-windows per row,
        # forwards per row, and measured == 0. A forward with a measurement is a different story.
        now = time.time()
        # 1,440 samples at 5 s = two hours, ending now: fresh (a stale log earns "insufficient
        # observation", which would mask the outcome under test).
        ambient = write_log([(now - 7195 + i * 5, 0.30) for i in range(1440)])
        watch = write_log([])
        attempts = write_log([])
        past = (2026, 11, 1, 0, 0, 0)
        try:
            clean = ambient_map.horizon_status(ambient, end=past, watch_log=watch, attempt_log=attempts)
            self.assertIn("no schedulable window observed", clean["outcome"])
            with open(watch, "w") as handle:
                handle.write(json.dumps({"row": "gate3:d89f689+:0.38", "action": "launch"}) + "\n")
            unable = ambient_map.horizon_status(ambient, end=past, watch_log=watch, attempt_log=attempts)
            self.assertIn("windows-observed-instrument-unable", unable["outcome"])
            self.assertIn("yes_windows=", unable["outcome"])
            self.assertIn("measured=0", unable["outcome"])
        finally:
            for path in (ambient, watch, attempts):
                os.unlink(path)

    def test_mode_share_is_definitional(self):
        # "majority in-gate" is the definition, not a tuned cutoff: exactly half is schedulable.
        self.assertEqual(ambient_map.MODE_SHARE, 0.50)
        self.assertEqual(ambient_map.TARGET_LOAD, 0.72)
        self.assertEqual(ambient_map.BAND, 0.30)


if __name__ == "__main__":
    unittest.main()
