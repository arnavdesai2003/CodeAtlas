"""Git subprocess policy without network, clones or services."""
import subprocess
import unittest
from unittest.mock import Mock, patch

from app.indexer import git


class GitRunnerTests(unittest.TestCase):
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

