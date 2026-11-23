import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, ".githooks", "post-commit")


class PostCommitHookTest(unittest.TestCase):
    """The hook warns, never refuses — and it must fire on the exact signature of the twice-made mistake."""

    def run_hook(self, directory):
        return subprocess.run(["sh", HOOK], cwd=directory, capture_output=True, text=True)

    def test_a_dirty_tree_after_a_commit_warns_and_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
            with open(os.path.join(directory, "unstaged.txt"), "w") as handle:
                handle.write("edited but never added\n")
            result = self.run_hook(directory)
        self.assertEqual(result.returncode, 0)             # warn, never refuse
        self.assertIn("unstaged after this commit", result.stderr)

    def test_a_clean_tree_is_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
            result = self.run_hook(directory)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("unstaged after this commit", result.stderr)

    def test_the_hook_is_executable_and_installed_through_one_redirect(self):
        self.assertTrue(os.access(HOOK, os.X_OK))
        configured = subprocess.run(["git", "config", "core.hooksPath"], cwd=ROOT,
                                    capture_output=True, text=True).stdout.strip()
        self.assertEqual(configured, ".githooks")           # one redirect covers every hook


if __name__ == "__main__":
    unittest.main()
