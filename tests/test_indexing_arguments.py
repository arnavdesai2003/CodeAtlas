"""Indexing command validation must precede session creation."""
import io
from contextlib import redirect_stderr, redirect_stdout
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from scripts import index_elasticsearch, index_symbols, index_all_elasticsearch, index_all_symbols


class IndexingArgumentTests(unittest.TestCase):
    def test_batch_help_never_opens_session_or_indexes(self):
        for command in (index_all_elasticsearch, index_all_symbols):
            with patch.object(command, "SessionLocal") as session, \
                 patch.object(command, "main") as main, redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    command.cli(["--help"])
            self.assertEqual(result.exception.code, 0)
            self.assertIn("all registered repositories", output.getvalue())
            session.assert_not_called()
            main.assert_not_called()

    def test_unsupported_batch_arguments_never_start_full_indexing(self):
        for command in (index_all_elasticsearch, index_all_symbols):
            for arguments in (["--repository-id", "6"], ["--unsupported"], ["unexpected"]):
                with self.subTest(command=command.__name__, arguments=arguments), \
                     patch.object(command, "SessionLocal") as session, \
                     patch.object(command, "main") as main, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        command.cli(arguments)
                self.assertEqual(result.exception.code, 2)
                session.assert_not_called()
                main.assert_not_called()

    def test_single_cli_failure_is_sanitized_and_nonzero(self):
        for command in (index_elasticsearch, index_symbols):
            output = io.StringIO()
            with patch.object(command, "main", side_effect=RuntimeError("private backend token")), redirect_stdout(output):
                self.assertEqual(command.cli([]), 1)
            self.assertIn("RuntimeError", output.getvalue())
            self.assertNotIn("private backend token", output.getvalue())

    def test_batch_session_failure_is_sanitized_and_nonzero(self):
        for command in (index_all_elasticsearch, index_all_symbols):
            output = io.StringIO()
            with patch.object(command, "SessionLocal", side_effect=RuntimeError("private database URL")), redirect_stdout(output):
                self.assertEqual(command.cli([]), 1)
            self.assertNotIn("private database URL", output.getvalue())

    def test_batch_continues_after_failure_without_echoing_exception(self):
        for command, operation in ((index_all_elasticsearch, "index_repository_in_elasticsearch"),
                                   (index_all_symbols, "index_repository_symbols")):
            db = Mock()
            db.query.return_value.order_by.return_value.all.return_value = [
                SimpleNamespace(id=1, name="first"), SimpleNamespace(id=2, name="second")]
            output = io.StringIO()
            with patch.object(command, "SessionLocal", return_value=db), \
                 patch.object(command, operation, side_effect=[RuntimeError("private credentials"),
                    {"symbols_indexed": 1, "parsed_files": 1, "skipped_files": 0}]) as index, redirect_stdout(output):
                with self.assertRaises(SystemExit) as failure:
                    command.cli([])
                self.assertEqual(str(failure.exception), "1 repositories failed; retry pending work.")
            self.assertEqual(index.call_count, 2)
            self.assertNotIn("private credentials", output.getvalue())
            self.assertIn("RuntimeError", output.getvalue())
            if command is index_all_symbols:
                self.assertIn("scripts.index_elasticsearch --repository-id 2", output.getvalue())
            db.close.assert_called_once()

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
            output = io.StringIO()
            with patch.object(command, "SessionLocal", return_value=db), \
                 patch.object(command, operation, return_value={}) as index, redirect_stdout(output):
                command.main(6)
            index.assert_called_once_with(db=db, repository_id=6)
            db.close.assert_called_once()
            if command is index_symbols:
                self.assertIn("Elasticsearch publication pending", output.getvalue())
                self.assertIn("scripts.index_elasticsearch --repository-id 6", output.getvalue())

    def test_operation_failure_still_closes_session(self):
        for command, operation in ((index_elasticsearch, "index_repository_in_elasticsearch"),
                                   (index_symbols, "index_repository_symbols")):
            db = Mock()
            with patch.object(command, "SessionLocal", return_value=db), \
                 patch.object(command, operation, side_effect=RuntimeError("fixture failure")):
                with self.assertRaises(RuntimeError):
                    command.main(6)
            db.close.assert_called_once()
