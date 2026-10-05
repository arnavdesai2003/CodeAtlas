"""HTTP verifier must not start a server for help or invalid arguments."""
import contextlib
import io
import unittest
from unittest.mock import patch
from scripts import verify_http_receipt as command


class HttpReceiptCommandTests(unittest.TestCase):
    def test_arguments_precede_owned_server_creation(self):
        with patch.object(command, "verify_http_receipt") as verify:
            for argv, status in ((["--help"], 0), (["--unknown"], 2)):
                with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as outcome:
                        command.main(argv)
                    self.assertEqual(outcome.exception.code, status)
            verify.assert_not_called()

    def test_success_preserves_probe_execution(self):
        with patch.object(command, "verify_http_receipt") as verify:
            self.assertEqual(command.main([]), 0)
            verify.assert_called_once_with()

    def test_probe_or_cleanup_failure_has_sanitized_nonzero_outcome(self):
        output = io.StringIO()
        with patch.object(command, "verify_http_receipt", side_effect=RuntimeError("private detail")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(command.main([]), 1)
        self.assertNotIn("private detail", output.getvalue())
        self.assertNotIn('"completed": true', output.getvalue())
        self.assertIn("no success claimed", output.getvalue())
