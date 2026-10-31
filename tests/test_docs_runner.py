"""Milestone 33 tests: the docs runner, including its negative controls.

A runner that reports success on a broken block is worse than no runner, so every test here pairs a
good document with a deliberately broken one and asserts the exit code differs.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import docs_runner  # noqa: E402


def write(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class TestDocsRunner(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def run_one(self, text):
        return docs_runner.run_document(write(self.directory, "doc.md", text))

    def test_good_python_block_passes(self):
        failures, ran, skipped = self.run_one("```python\nprint('hello')\n```\n")
        self.assertEqual((failures, ran), ([], 1))

    def test_broken_python_block_fails(self):
        failures, ran, _ = self.run_one("```python\nraise SystemExit(3)\n```\n")
        self.assertEqual(len(failures), 1, "a block that exits non-zero must be reported")

    def test_broken_sql_block_fails(self):
        failures, _, _ = self.run_one("```sql\nSELECT * FROM nope;\n```\n")
        self.assertEqual(len(failures), 1)

    def test_sql_block_with_trailing_comment_passes(self):
        failures, _, _ = self.run_one("```sql\nCREATE TABLE t (a INTEGER);\n"
                                      "SELECT * FROM t;   -- empty table\n```\n")
        self.assertEqual(failures, [])

    def test_sql_block_must_seed_its_own_tables(self):
        # Two blocks, the second depending on the first: the runner must fail the second, because
        # documents get reordered and a block that needs a neighbour is a latent break.
        text = ("```sql\nCREATE TABLE t (a INTEGER);\n```\n\n"
                "```sql\nINSERT INTO t VALUES (1);\n```\n")
        failures, _, _ = self.run_one(text)
        self.assertEqual(len(failures), 1)

    def test_non_runnable_languages_are_skipped_and_counted(self):
        failures, ran, skipped = self.run_one("```text\nnot code\n```\n```bash\necho hi\n```\n")
        self.assertEqual((failures, ran, skipped), ([], 0, 2))

    def test_real_documents_pass(self):
        failures, ran, _ = docs_runner.run_document(os.path.join(ROOT, "README.md"))
        self.assertEqual(failures, [], "README's runnable block must work")
        self.assertGreaterEqual(ran, 1)


if __name__ == "__main__":
    unittest.main()
