"""Indexing command validation must precede session creation."""
import io
from contextlib import redirect_stderr, redirect_stdout
import unittest
from unittest.mock import Mock, patch
from scripts import index_elasticsearch, index_symbols


class IndexingArgumentTests(unittest.TestCase):
    def test_invalid_cli_ids_exit_before_session_creation(self):
        for command in (index_elasticsearch, index_symbols):
            with patch.object(command, "SessionLocal") as sessions:
                for value in ("0", "-1", "2147483648", "1.5", "true"):
                    with self.subTest(command=command.__name__, value=value), redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as failure:
                            command.cli(["--repository-id", value])
                        self.assertEqual(failure.exception.code, 2)
                sessions.assert_not_called()

    def test_direct_invalid_ids_fail_before_session_creation(self):
        for command in (index_elasticsearch, index_symbols):
            with patch.object(command, "SessionLocal") as sessions:
                for value in (0, -1, True, 1.0, 2147483648):
                    with self.assertRaises(ValueError):
                        command.main(value)
                sessions.assert_not_called()

    def test_cli_preserves_default_and_explicit_boundary_ids(self):
        for command in (index_elasticsearch, index_symbols):
            with patch.object(command, "main") as main:
                for arguments, expected in (([], 1), (["--repository-id", "6"], 6),
                                             (["--repository-id", "2147483647"], 2147483647)):
                    command.cli(arguments)
                    main.assert_called_with(expected)

    def test_selected_repository_and_session_cleanup(self):
        for command, operation in ((index_elasticsearch, "index_repository_in_elasticsearch"),
                                   (index_symbols, "index_repository_symbols")):
            db = Mock()
            with patch.object(command, "SessionLocal", return_value=db), \
                 patch.object(command, operation, return_value={}) as index, redirect_stdout(io.StringIO()):
                command.main(6)
            index.assert_called_once_with(db=db, repository_id=6)
            db.close.assert_called_once()

    def test_operation_failure_still_closes_session(self):
        for command, operation in ((index_elasticsearch, "index_repository_in_elasticsearch"),
                                   (index_symbols, "index_repository_symbols")):
            db = Mock()
            with patch.object(command, "SessionLocal", return_value=db), \
                 patch.object(command, operation, side_effect=RuntimeError("fixture failure")):
                with self.assertRaises(RuntimeError):
                    command.main(6)
            db.close.assert_called_once()
