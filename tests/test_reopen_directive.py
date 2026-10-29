"""Negative control for the --directive: reopen machinery (ratified at the M4/M5 reviews).

The mechanism must be PROVEN able to fail: break the reopen step on either engine and the
constraints-survive case must FAIL. Without this, a silently inert directive turns the four
reopen corpus cases into permanent green no-ops — the exact trap the parked t1b draft fell
into by accident (its reopen cases passed under a loader that treated the markers as
comments and never tested anything).

Also guards the loader: a misspelled or unknown directive must fail loudly, never become an
inert comment, and provenance comments (`-- src:`) must remain comments.
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import behavior_conformance as bc  # noqa: E402
from minidb.engine import Database  # noqa: E402
from minidb.sql.parser import parse  # noqa: E402

# The constraints-survive case from tests/corpus/t1b.txt, in phase form:
# schema + valid row, reopen, then a violating insert (both engines must reject it
# identically). The honest outcome is ERROR AGREEMENT: SQLite's CLI aborts the phase on
# the violating insert, so no SELECT runs — "the constraint survives" is observed as
# both engines rejecting identically AFTER the reopen.
CONSTRAINT_CASE = [
    ["CREATE TABLE u (n INTEGER CHECK (n > 0))", "INSERT INTO u VALUES (5)"],
    ["INSERT INTO u VALUES (0)", "SELECT n FROM u"],
]

# Rows-visible-after-reopen, in phase form: the shape whose honest outcome is rows.
ROWS_CASE = [
    ["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (5)"],
    ["SELECT id FROM t"],
]


def sqlite_script(statements):
    return ";\n".join(statements) + ";\n"


class TestReopenMechanism(unittest.TestCase):
    def run_phases(self, case):
        with tempfile.TemporaryDirectory() as tmp:
            bc.REOPEN_TMPDIR = tmp
            try:
                expected = bc.run_sqlite(case)
                actual = bc.run_minidb(case)
            finally:
                bc.REOPEN_TMPDIR = None
        return bc.compare(expected, actual), expected, actual

    def test_honest_reopen_passes_with_rows(self):
        (ok, detail), expected, actual = self.run_phases(ROWS_CASE)
        self.assertTrue(ok, detail)
        # and the row must actually be there — an empty-but-agreeing result would be
        # a pass with nothing tested
        self.assertEqual(actual["rows"], [{"id": 5}], actual)
        self.assertEqual(expected["rows"], [{"id": 5}], expected)

    def test_honest_constraint_case_agrees_on_error_after_reopen(self):
        (ok, detail), expected, actual = self.run_phases(CONSTRAINT_CASE)
        self.assertTrue(ok, detail)
        self.assertIn("error", actual, actual)  # the violating insert is rejected post-reopen
        self.assertEqual(actual["error"], expected["error"], detail)

    def test_minidb_losing_data_across_reopen_fails(self):
        """Storage 'reopens' a fresh file instead of the written one: the case must FAIL."""
        with tempfile.TemporaryDirectory() as tmp:
            written = os.path.join(tmp, "written.db")
            db = Database(written)
            try:
                for statement in CONSTRAINT_CASE[0]:
                    db.execute(parse(statement))
            finally:
                db.close()
            bc.REOPEN_TMPDIR = tmp
            try:
                expected = bc.run_sqlite(CONSTRAINT_CASE)
                # MiniDB side: final phase against a FRESH file, simulating data loss
                db2 = Database(os.path.join(tmp, "fresh.db"))
                try:
                    try:
                        columns, rows = db2.execute(parse(CONSTRAINT_CASE[-1][0]))
                        keys = list(columns)
                        actual_rows = {"columns": keys,
                                       "rows": [dict(zip(keys, r)) for r in rows]}
                    except Exception as exc:  # engine error is still a loss signal
                        actual_rows = {"error": str(exc)}
                finally:
                    db2.close()
            finally:
                bc.REOPEN_TMPDIR = None
        ok, detail = bc.compare(expected, actual_rows)
        self.assertFalse(ok, "constraint-survive case passed despite data loss: " + detail)

    def test_sqlite_losing_rows_across_reopen_fails(self):
        """Reference side reopening the wrong file: the case must FAIL, not silently agree."""
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["sqlite3", os.path.join(tmp, "written.db")],
                           input=sqlite_script(CONSTRAINT_CASE[0]),
                           capture_output=True, text=True, check=True)
            broken = subprocess.run(
                ["sqlite3", "-json", os.path.join(tmp, "fresh.db")],
                input=sqlite_script(CONSTRAINT_CASE[1]),
                capture_output=True, text=True)
            expected = {"error": bc.clean_error(broken.stderr)}
            bc.REOPEN_TMPDIR = tmp
            try:
                actual = bc.run_minidb(CONSTRAINT_CASE)
            finally:
                bc.REOPEN_TMPDIR = None
        ok, detail = bc.compare(expected, actual)
        self.assertFalse(ok, "case passed despite reference-side data loss: " + detail)


class TestDirectiveLoader(unittest.TestCase):
    def corpus(self, content):
        fh = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        fh.write(content)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_reopen_marker_splits_phases(self):
        cases = bc.load_cases(self.corpus(
            "CREATE TABLE t (id INTEGER)\nINSERT INTO t VALUES (1)\n"
            "--directive: reopen\nSELECT id FROM t\n"))
        self.assertEqual(len(cases), 1)
        directives, phases = cases[0]
        self.assertEqual(len(phases), 2)
        self.assertEqual(phases[1], ["SELECT id FROM t"])

    def test_double_reopen_splits_three_phases(self):
        cases = bc.load_cases(self.corpus(
            "CREATE TABLE t (id INTEGER)\n--directive: reopen\nINSERT INTO t VALUES (1)\n"
            "--directive: reopen\nSELECT id FROM t\n"))
        self.assertEqual(len(cases[0][1]), 3)

    def test_unknown_directive_value_fails_loudly(self):
        with self.assertRaises(SystemExit):
            bc.load_cases(self.corpus("--directive: reopn\nSELECT 1\n"))

    def test_near_miss_marker_fails_loudly(self):
        with self.assertRaises(SystemExit):
            bc.load_cases(self.corpus("--reopn: reopen\nSELECT 1\n"))

    def test_reopen_without_statements_after_fails_loudly(self):
        with self.assertRaises(SystemExit):
            bc.load_cases(self.corpus("CREATE TABLE t (id INTEGER)\n--directive: reopen\n"))

    def test_empty_corpus_file_is_a_clean_error_not_a_traceback(self):
        with self.assertRaises(SystemExit):
            bc.load_cases(self.corpus(""))

    def test_comment_only_corpus_file_is_a_clean_error(self):
        with self.assertRaises(SystemExit):
            bc.load_cases(self.corpus("-- just a header, no cases\n"))

    def test_provenance_comments_stay_comments(self):
        cases = bc.load_cases(self.corpus("-- src: authored\n-- tier: t1\nSELECT 1\n"))
        self.assertEqual(cases, [(set(), [["SELECT 1"]])])

    def test_prose_with_directive_word_stays_comment(self):
        cases = bc.load_cases(self.corpus(
            "-- reopen: rows written before close are visible after reopen\nSELECT 1\n"))
        self.assertEqual(len(cases[0][1]), 1)  # one phase, no reopen


class TestSortDirective(unittest.TestCase):
    def test_sort_normalises_both_sides(self):
        rows_a = [{"id": 2}, {"id": 1}]
        rows_b = [{"id": 1}, {"id": 2}]
        self.assertEqual(
            bc.sort_rows({"columns": ["id"], "rows": rows_a})["rows"],
            bc.sort_rows({"columns": ["id"], "rows": rows_b})["rows"])


if __name__ == "__main__":
    unittest.main()
