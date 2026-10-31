"""Milestone 29 tests: the shell's declared dot-command subset and CSV round-trip."""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def run_shell(script, db_path=None):
    argv = [sys.executable, "-m", "minidb.cli"] + ([db_path] if db_path else [])
    return subprocess.run(argv, input=script, capture_output=True, text=True, cwd=ROOT)


class TestShell(unittest.TestCase):
    def test_select_prints_pipe_separated_rows(self):
        result = run_shell("CREATE TABLE t (a INTEGER, b TEXT);\n"
                           "INSERT INTO t VALUES (1,'x'),(2,NULL);\n"
                           "SELECT * FROM t ORDER BY a;\n")
        self.assertEqual(result.stdout.strip().splitlines(), ["1|x", "2|"])

    def test_tables_and_schema(self):
        result = run_shell("CREATE TABLE alpha (id INTEGER);\n"
                           "CREATE TABLE beta (id INTEGER);\n"
                           ".tables\n.schema alpha\n")
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[0], "alpha beta")
        self.assertEqual(lines[1], "CREATE TABLE alpha (id INTEGER);")

    def test_help_names_the_gate_set(self):
        result = run_shell(".help\n")
        for command in (".tables", ".schema", ".quit"):
            self.assertIn(command, result.stdout)

    def test_unknown_dot_command_is_reported_not_ignored(self):
        result = run_shell(".vacuum\n")
        self.assertIn("unknown command", result.stdout)

    def test_errors_are_printed_not_crashed(self):
        result = run_shell("SELECT * FROM missing;\n")
        self.assertIn("no such table", result.stdout)
        self.assertEqual(result.returncode, 0)

    def test_database_file_persists_between_runs(self):
        path = os.path.join(tempfile.mkdtemp(), "cli.db")
        run_shell("CREATE TABLE t (a INTEGER);\nINSERT INTO t VALUES (7);\n", path)
        result = run_shell("SELECT a FROM t;\n", path)
        self.assertEqual(result.stdout.strip(), "7")

    def test_csv_round_trip(self):
        directory = tempfile.mkdtemp()
        source = os.path.join(directory, "in.csv")
        exported = os.path.join(directory, "out.csv")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("a,b\n1,x\n2,y\n")
        script = (f"CREATE TABLE t (a INTEGER, b TEXT);\n.import {source} t\n"
                  f".export t {exported}\n")
        result = run_shell(script)
        self.assertIn("imported 2 rows", result.stdout)
        self.assertIn("exported 2 rows", result.stdout)
        with open(source, encoding="utf-8") as handle:
            before = handle.read().strip()
        with open(exported, encoding="utf-8") as handle:
            after = handle.read().strip()
        self.assertEqual(before.replace("\r", ""), after.replace("\r", ""),
                         "CSV -> table -> CSV must round-trip")


if __name__ == "__main__":
    unittest.main()
