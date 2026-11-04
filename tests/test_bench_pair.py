"""The pairing step's own gates (tools/bench_pair.py), exercised on fixtures rather than a run.

The tool exists because a benchmark process can exit 0 with an artifact that exists and contains
nothing; the 100k pair is the run that will use it, so the positive path cannot wait for that run
to be tested. Nine synthetic workloads are enough to pin every branch: completeness, self-
consistency, answer mismatch, and the table itself, including the reference-delta column that
makes a denominator-only move visible.
"""

import contextlib
import importlib.util
import io
import os
import statistics
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location("bench_pair", os.path.join(ROOT, "tools", "bench_pair.py"))
bench_pair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench_pair)

RATIOS = {"W1": 50479.0, "W2": 900.0, "W3": 77.6, "W4": 144.2, "W5": 70.0,
          "W6": 16.2, "W7": 30.0, "W8": 40.0, "W10": 60.0}


def band_percent(values):
    median = statistics.median(values)
    return 0.0 if median == 0 else (max(values) - min(values)) / median * 100.0


def write_run(path, minidb_shift=1.0, sqlite_shift=1.0, output="OK", drop=(), ratio_override=None,
              discard=(), errored=(), bare=(), n=7, omit=(), ratio_min_override=None,
              band_override=None, loadavg=2.0, raw_label="raw", header_n=None, demo=False):
    """A file in the harness's printed shape: header, then per-workload blocks.

    The two sides move independently so a denominator-only move can be modelled: that is the
    artefact the reference-delta column exists to expose. `n` sets both legs' sample count and
    `omit` drops individual gate fields, so the four certification failure modes are fixtures
    rather than mutations of a real artifact. `raw_label="raw7"` reproduces a pre-label-fix
    artifact; `header_n` lets the header disagree with the printed lists on purpose; `demo`
    writes the same self-declaration `leg_report`'s fixture carries.
    """
    declared = n if header_n is None else header_n
    marker = "  demo=true" if demo else ""
    lines = ["BENCH first-full-run  module-sqlite 3.53.1  N=%d  scale=20000  lookups=1000%s"
             % (declared, marker), ""]
    for name, ratio in RATIOS.items():
        if name in drop:
            continue
        if name in discard:
            lines.append(f"{name}  synthetic {name}          DISCARDED: outputs differ (first mismatch: [(1,)] vs [(2,)]")
            continue
        if name in errored:
            lines.append(f'{name}  synthetic {name}          ERROR: OperationalError("boom")')
            continue
        if name in bare:
            lines.append(f"{name}  synthetic {name}          rows=1")
            continue
        base = 0.02 if name == "W1" else 10.0
        sqlite_ms = base * sqlite_shift
        minidb_ms = base * ratio * minidb_shift
        shown_ratio = (ratio_override or {}).get(name, minidb_ms / sqlite_ms)
        samples_sq, samples_md = [sqlite_ms] * n, [minidb_ms] * n
        shown_min = (ratio_min_override or {}).get(name, statistics.median(samples_md) / min(samples_sq))
        band_sq = (band_override or {}).get(("sq", name), band_percent(samples_sq))
        band_md = (band_override or {}).get(("md", name), band_percent(samples_md))
        bench_header = f"{name}  synthetic {name}          rows=1  loadavg={loadavg:.2f}"
        lines += [bench_header,
                  f"     sqlite_ms median={statistics.median(samples_sq):.2f}  {raw_label}=[ "
                  f"{' '.join('%.2f' % x for x in samples_sq)} ]  band={band_sq:.1f}%"
                  if "band" not in omit else
                  f"     sqlite_ms median={statistics.median(samples_sq):.2f}  {raw_label}=[ "
                  f"{' '.join('%.2f' % x for x in samples_sq)} ]"]
        if "loads" not in omit:
            lines.append(f"     sqlite_loads=[ {' '.join('%.2f' % loadavg for _ in samples_sq)} ]"
                         f"  load_span=0.00")
        lines.append(f"     minidb_ms median={statistics.median(samples_md):.2f}  {raw_label}=[ "
                     f"{' '.join('%.2f' % x for x in samples_md)} ]  band={band_md:.1f}%"
                     if "band" not in omit else
                     f"     minidb_ms median={statistics.median(samples_md):.2f}  {raw_label}=[ "
                     f"{' '.join('%.2f' % x for x in samples_md)} ]")
        if "loads" not in omit:
            lines.append(f"     minidb_loads=[ {' '.join('%.2f' % loadavg for _ in samples_md)} ]"
                         f"  load_span=0.00")
        tail = f"     ratio={shown_ratio:.3f}  output: {output}"
        if "ratio_min" not in omit:
            tail += f"  ratio_min={shown_min:.3f}"
        lines.append(tail)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


class BenchPairTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.dir, name)

    def run_pair(self, before, after):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bench_pair.main([before, after])

    def test_a_complete_pair_pairs(self):
        before = write_run(self.path("b.txt"))
        after = write_run(self.path("a.txt"), minidb_shift=0.5)
        self.assertEqual(self.run_pair(before, after), 0)

    def test_a_file_without_the_harness_header_is_not_a_run(self):
        foreign = self.path("foreign.txt")
        with open(foreign, "w", encoding="utf-8") as handle:
            handle.write("BENCH x\nW3  synthetic          rows=1\n     ratio=1.000  output: OK\n")
        self.assertEqual(self.run_pair(foreign, write_run(self.path("a.txt"))), 2)

    def test_a_header_only_file_is_refused(self):
        empty = self.path("empty.txt")
        with open(empty, "w", encoding="utf-8") as handle:
            handle.write("BENCH first-full-run  module-sqlite 3.53.1  N=7  scale=20000  lookups=1000\n")
        self.assertEqual(self.run_pair(empty, write_run(self.path("a.txt"))), 2)

    def test_a_partial_file_names_what_is_missing(self):
        partial = write_run(self.path("p.txt"), drop=("W2", "W7"))
        self.assertEqual(self.run_pair(partial, write_run(self.path("a.txt"))), 2)

    def test_a_ratio_that_contradicts_its_medians_is_refused(self):
        broken = write_run(self.path("broken.txt"), ratio_override={"W3": 12.0})
        self.assertEqual(self.run_pair(broken, write_run(self.path("a.txt"))), 4)

    def test_an_answer_mismatch_is_refused(self):
        mismatched = write_run(self.path("m.txt"), output="MISMATCH")
        self.assertEqual(self.run_pair(mismatched, write_run(self.path("a.txt"))), 3)

    def test_a_discarded_workload_is_a_finding_not_a_hole(self):
        # Present but not a number: exit 3 (triage), never exit 2 (killed run).
        self.assertEqual(self.run_pair(write_run(self.path("d.txt"), discard=("W6",)),
                                       write_run(self.path("a.txt"))), 3)

    def test_an_errored_workload_is_a_finding_not_a_hole(self):
        self.assertEqual(self.run_pair(write_run(self.path("e.txt"), errored=("W7",)),
                                       write_run(self.path("a.txt"))), 3)

    def test_a_section_without_a_terminal_line_is_incomplete(self):
        self.assertEqual(self.run_pair(write_run(self.path("b.txt"), bare=("W3",)),
                                       write_run(self.path("a.txt"))), 2)

    def test_a_denominator_only_move_is_visible(self):
        # MiniDB's own medians are identical; only SQLite's moved, so every ratio halves.
        before = write_run(self.path("b.txt"))
        after = write_run(self.path("a.txt"), sqlite_shift=2.0)
        before_runs, after_runs = bench_pair.parse(before), bench_pair.parse(after)
        self.assertAlmostEqual(before_runs["W3"]["minidb_ms"], after_runs["W3"]["minidb_ms"])
        fold, word = bench_pair.verdict(before_runs["W3"]["ratio"], after_runs["W3"]["ratio"])
        self.assertAlmostEqual(fold, 2.0)
        self.assertEqual(word, "improved")   # the table's job is to show it, the reader's to judge


class GateCertificationTest(unittest.TestCase):
    """`--gate-only` certifies artifact fitness for a gate-2 call: completeness, self-consistency
    from the published samples, the frozen N floor, and the pre-load window. It never applies the
    60x threshold — that stays a gate-event read of the emitted `ratio_min`.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.dir, name)

    def gate(self, artifact, workloads="W1"):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bench_pair.main(["--gate-only", "--workloads", workloads, artifact])

    def test_a_complete_gate_artifact_passes(self):
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=11)), 0)

    def test_a_missing_ratio_min_fails_certification(self):
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=11, omit=("ratio_min",))), 2)

    def test_a_false_ratio_min_fails_by_recomputation(self):
        # W6's true ratio_min is 16.2; 18.0 is above it (so the min<=median invariant passes) and
        # 11% high (so the 1% recomputation tolerance cannot absorb it). Presence alone would pass.
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=11,
                                             ratio_min_override={"W6": 18.0}), workloads="W6"), 2)

    def test_an_inconsistent_band_fails_certification(self):
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=11,
                                             band_override={("md", "W1"): 42.0})), 2)

    def test_a_missing_load_trace_fails_certification(self):
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=11, omit=("loads",))), 2)

    def test_an_out_of_window_preload_fails_certification(self):
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=11, loadavg=3.5)), 2)

    def test_a_seven_sample_artifact_cannot_certify_a_gate_call(self):
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=7)), 2)

    def test_an_old_artifact_still_pairs_even_though_it_cannot_certify(self):
        old = write_run(self.path("old.txt"), n=7, omit=("ratio_min", "band", "loads"),
                        raw_label="raw7")
        self.assertEqual(self.gate(old), 2)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bench_pair.main([old, old]), 0)

    def test_a_header_that_disagrees_with_its_raw_lists_fails_certification(self):
        # 11 samples printed under a header that declares N=7: the label and the data disagree.
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=11, header_n=7)), 2)

    def test_the_neutral_raw_label_parses(self):
        runs = bench_pair.parse(write_run(self.path("g.txt"), n=11))
        self.assertEqual(len(runs["W1"]["raw_sq"]), 11)
        self.assertEqual(len(runs["W1"]["raw_md"]), 11)

    def test_the_load_span_field_parses(self):
        runs = bench_pair.parse(write_run(self.path("g.txt"), n=11))
        self.assertEqual(runs["W1"]["span_sq"], 0.0)
        self.assertEqual(runs["W1"]["span_md"], 0.0)

    def test_the_checker_does_not_apply_the_sixty_x_threshold(self):
        # Far above 60x and still certified: the checker asserts fitness, not the gate call.
        self.assertEqual(self.gate(write_run(self.path("g.txt"), n=11, minidb_shift=1000.0)), 0)


class DemoFixtureTest(unittest.TestCase):
    """A `demo=true` header is a fixture's own confession. It may be shown, never certified."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def path(self, name):
        return os.path.join(self.dir, name)

    def run_tool(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bench_pair.main(argv)

    def test_gate_certification_refuses_a_demo_fixture(self):
        self.assertEqual(self.run_tool(["--gate-only", "--workloads", "W1",
                                        write_run(self.path("d.txt"), n=11, demo=True)]), 2)

    def test_gate_certification_refuses_a_demo_even_when_the_escape_is_passed(self):
        self.assertEqual(self.run_tool(["--gate-only", "--allow-demo", "--workloads", "W1",
                                        write_run(self.path("d.txt"), n=11, demo=True)]), 2)

    def test_pairing_refuses_a_demo_fixture_without_the_flag(self):
        demo = write_run(self.path("d.txt"), demo=True)
        self.assertEqual(self.run_tool([demo, write_run(self.path("a.txt"))]), 2)

    def test_pairing_shows_a_demo_fixture_with_the_flag(self):
        demo = write_run(self.path("d.txt"), demo=True)
        self.assertEqual(self.run_tool(["--allow-demo", demo, write_run(self.path("a.txt"))]), 0)


if __name__ == "__main__":
    unittest.main()
