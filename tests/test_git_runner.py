"""Git subprocess policy without network, clones or services."""
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.indexer import git


class GitRunnerTests(unittest.TestCase):
    def test_real_nul_diff_preserves_tab_and_newline_filename(self):
        from app.indexer.incremental import parse_git_diff
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git.git_output("init", str(root))
            name = "tab\tline\n.py"
            (root / name).write_text("first")
            git.repository_git_output(root, "add", "--", name)
            git.repository_git_output(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                      "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
            (root / name).write_text("second")
            output = git.repository_git_output(root, "diff", "--name-status", "-z", "HEAD")
            self.assertEqual(parse_git_diff(output, nul=True), [{"status": "M", "path": name}])

    def test_missing_child_metadata_cannot_discover_parent_repository(self):
        from app.indexer.errors import UnsafeClonePath
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            git.git_output("init", str(parent))
            child = parent / "child"
            child.mkdir()
            with patch.object(git.subprocess, "run") as run:
                with self.assertRaises(UnsafeClonePath):
                    git.repository_git_output(child, "reset", "--hard")
            run.assert_not_called()
            (child / ".git").symlink_to(parent / ".git", target_is_directory=True)
            with self.assertRaises(UnsafeClonePath):
                git.repository_git_output(child, "status")

    def test_explicit_repository_paths_and_commondir_rejection(self):
        from app.indexer.errors import UnsafeClonePath
        with tempfile.TemporaryDirectory() as directory:
            chosen = Path(directory)
            git.git_output("init", str(chosen))
            self.assertEqual(Path(git.repository_git_output(chosen, "rev-parse", "--show-toplevel")).resolve(), chosen.resolve())
            (chosen / ".git/commondir").write_text("../outside")
            with self.assertRaises(UnsafeClonePath):
                git.repository_git_output(chosen, "status")
    def test_repository_overrides_removed_only_from_child_environment(self):
        overrides = {name: "redirected" for name in git.REPOSITORY_ENVIRONMENT}
        overrides["GIT_ASKPASS"] = "trusted-helper"
        with patch.dict(git.os.environ, overrides), \
             patch.object(git.subprocess, "run", return_value=Mock(stdout="")) as run:
            git.git_output("--version")
            child = run.call_args.kwargs["env"]
            self.assertTrue(git.REPOSITORY_ENVIRONMENT.isdisjoint(child))
            self.assertEqual(child["GIT_ASKPASS"], "trusted-helper")
            self.assertEqual(git.os.environ["GIT_DIR"], "redirected")

    def test_real_git_ignores_parent_repository_and_worktree_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            chosen, other = base / "chosen", base / "other"
            chosen.mkdir()
            other.mkdir()
            git.git_output("init", str(chosen))
            git.git_output("init", str(other))
            with patch.dict(git.os.environ, {"GIT_DIR": str(other / ".git"),
                                            "GIT_WORK_TREE": str(other),
                                            "GIT_INDEX_FILE": str(base / "outside-index")}):
                observed = git.git_output("-C", str(chosen), "rev-parse", "--show-toplevel")
                self.assertEqual(Path(observed).resolve(), chosen.resolve())
                git.git_output("-C", str(chosen), "status", "--porcelain")
            self.assertFalse((base / "outside-index").exists())

    def test_runner_is_bounded_noninteractive_and_preserves_environment(self):
        with patch.dict(git.os.environ, {"GIT_TERMINAL_PROMPT": "1", "CODEATLAS_TEST": "keep"}), \
             patch.object(git.settings, "git_timeout_seconds", 7), \
             patch.object(git.subprocess, "run", return_value=Mock(stdout=" commit\n")) as run:
            self.assertEqual(git.git_output("-C", "clone path", "rev-parse", "HEAD"), "commit")
            kwargs = run.call_args.kwargs
            self.assertEqual(run.call_args.args[0], ["git", "-C", "clone path", "rev-parse", "HEAD"])
            self.assertEqual(kwargs["timeout"], 7)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(kwargs["env"]["CODEATLAS_TEST"], "keep")
            self.assertEqual(git.os.environ["GIT_TERMINAL_PROMPT"], "1")
