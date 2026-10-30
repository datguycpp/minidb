"""Guards for the conformance harness itself — a harness that silently drops cases would
report a perfect score forever, which is worse than reporting a bad one."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import syntax_conformance as sc  # noqa: E402


class TestCorpusLoading(unittest.TestCase):
    def test_negative_markers_are_not_skipped_as_comments(self):
        cases = sc.load_corpus(sc.CORPUS)
        negatives = [c for c in cases if c["reject_marker"]]
        self.assertGreaterEqual(len(negatives), 10, "reject-marked cases must survive comment filtering")

    def test_marker_prefix_is_stripped_from_the_statement(self):
        cases = sc.load_corpus(sc.CORPUS)
        for case in cases:
            self.assertFalse(case["statement"].startswith("#!"), case["statement"])

    def test_corpus_is_not_empty(self):
        self.assertGreater(len(sc.load_corpus(sc.CORPUS)), 30)


class TestReferenceClassification(unittest.TestCase):
    def test_semantic_error_counts_as_syntactically_valid(self):
        # Table does not exist, so SQLite fails at prepare time, not parse time.
        ok, note = sc.sqlite_syntax_ok("SELECT * FROM no_such_table")
        self.assertTrue(ok, note)

    def test_real_syntax_error_is_rejected(self):
        ok, note = sc.sqlite_syntax_ok("SELECT * FROM")
        self.assertFalse(ok)
        self.assertTrue(any(w in note.lower() for w in ("syntax", "incomplete input")), note)

    def test_harness_detects_disagreement(self):
        # Set operations are valid SQLite and still outside the supported subset (M13), so the
        # disagreement must surface rather than be swallowed as agreement.
        sqlite_ok, _ = sc.sqlite_syntax_ok("SELECT 1 UNION SELECT 2")
        minidb_ok, _ = sc.minidb_syntax_ok("SELECT 1 UNION SELECT 2")
        self.assertTrue(sqlite_ok)
        self.assertFalse(minidb_ok)


if __name__ == "__main__":
    unittest.main()
