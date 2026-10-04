"""Batch ingestion failures must not masquerade as successful skips."""
import io
from contextlib import redirect_stdout, redirect_stderr
import unittest
from unittest.mock import Mock, patch
from app.indexer.errors import RepositoryConflict
from scripts import batch_ingest as command


class BatchIngestCommandTests(unittest.TestCase):
    def test_help_and_unknown_arguments_do_not_open_sessions(self):
        with patch.object(command, "SessionLocal") as session:
            for arguments, expected in ((["--help"], 0), (["--unsupported"], 2)):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        command.cli(arguments)
                self.assertEqual(result.exception.code, expected)
            session.assert_not_called()

    def test_failure_continues_then_exits_nonzero_without_raw_details(self):
        db = Mock()
        output = io.StringIO()
        with patch.object(command, "SessionLocal", return_value=db), \
             patch.object(command, "REPOSITORIES", ["https://github.com/test/first", "https://github.com/test/second"]), \
             patch.object(command, "ingest_repository", side_effect=[RuntimeError("private token"),
                {"repository_id": 2, "source_files_discovered": 1}]) as ingest, redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:
                command.cli([])
        self.assertIn("1 repositories failed", str(result.exception))
        self.assertEqual(ingest.call_count, 2)
        self.assertIn("Repository ID: 2", output.getvalue())
        self.assertNotIn("private token", output.getvalue())
        db.rollback.assert_called_once()
        db.close.assert_called_once()

    def test_conflicts_and_internal_value_errors_are_failures(self):
        for error in (RepositoryConflict("private clone path"), ValueError("private parser details")):
            db = Mock()
            output = io.StringIO()
            with patch.object(command, "SessionLocal", return_value=db), \
                 patch.object(command, "REPOSITORIES", ["https://github.com/test/fixture"]), \
                 patch.object(command, "ingest_repository", side_effect=error), redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    command.cli([])
            self.assertNotIn(str(error), output.getvalue())
            self.assertNotIn("Skipped", output.getvalue())
            db.close.assert_called_once()

    def test_session_setup_failure_is_sanitized_and_nonzero(self):
        output = io.StringIO()
        with patch.object(command, "SessionLocal", side_effect=RuntimeError("private database URL")), redirect_stdout(output):
            self.assertEqual(command.cli([]), 1)
        self.assertNotIn("private database URL", output.getvalue())
