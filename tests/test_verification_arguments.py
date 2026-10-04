"""Verification argument handling must precede scratch/service operations."""
import io
from contextlib import redirect_stdout, redirect_stderr
import unittest
from unittest.mock import patch
from scripts import (verify_cache_protocol, verify_publication_protocol,
                     verify_writer_locks, verify_writer_processes)

COMMANDS = ((verify_cache_protocol, "verify_cache_protocol"),
            (verify_publication_protocol, "verify_publication_protocol"),
            (verify_writer_locks, "verify_writer_locks"),
            (verify_writer_processes, "verify_writer_processes"))


class VerificationArgumentTests(unittest.TestCase):
    def test_help_exits_before_verification(self):
        for command, operation in COMMANDS:
            with self.subTest(command=command.__name__), patch.object(command, operation) as verify, \
                 redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    command.main(["--help"])
                self.assertEqual(result.exception.code, 0)
                self.assertIn("usage:", output.getvalue())
                verify.assert_not_called()

    def test_unknown_arguments_exit_before_verification(self):
        for command, operation in COMMANDS:
            for arguments in (["--unsupported"], ["unexpected"]):
                with self.subTest(command=command.__name__, arguments=arguments), \
                     patch.object(command, operation) as verify, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        command.main(arguments)
                    self.assertEqual(result.exception.code, 2)
                    verify.assert_not_called()
