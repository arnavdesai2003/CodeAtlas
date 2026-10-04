"""Offline failure checks for process coordination verification."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from scripts import verify_writer_processes as protocol


class WriterProcessProtocolTests(unittest.TestCase):
    def test_handshake_timeout_does_not_read(self):
        pipe = Mock()
        pipe.poll.return_value = False
        with self.assertRaises(RuntimeError):
            protocol._message(pipe, "held")
        pipe.recv.assert_not_called()

    def test_failed_handshake_is_rejected(self):
        pipe = Mock()
        pipe.recv.return_value = {"status": "failed", "error": "private"}
        with self.assertRaises(RuntimeError):
            protocol._message(pipe, "held")

    def test_stop_escalates_only_to_owned_process(self):
        process = Mock()
        process.is_alive.side_effect = [True, True, False]
        protocol._stop(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.join.call_count, 2)

    def test_stop_failure_blocks_success(self):
        process = Mock()
        process.is_alive.return_value = True
        with self.assertRaises(RuntimeError):
            protocol._stop(process)

    def test_release_timeout_fails(self):
        with patch.object(protocol, "generation_maintenance_lock",
                          side_effect=protocol.RepositorySyncInProgress()), \
             patch.object(protocol.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(RuntimeError, "remained"):
                protocol._await_release(Mock(), 1)

    def test_backend_failure_is_not_retried(self):
        with patch.object(protocol, "generation_maintenance_lock", side_effect=ConnectionError), \
             patch.object(protocol.time, "sleep") as sleep:
            with self.assertRaises(ConnectionError):
                protocol._await_release(Mock(), 1)
        sleep.assert_not_called()

    def test_owner_failure_is_sanitized_and_pipe_closed(self):
        pipe = Mock()
        with patch.object(protocol, "SessionLocal", side_effect=RuntimeError("secret")):
            protocol._owner(pipe, 1, False)
        pipe.send.assert_called_once_with({"status": "failed", "error": "RuntimeError"})
        pipe.close.assert_called_once()

    def test_cli_failure_is_nonzero_and_sanitized(self):
        output = io.StringIO()
        with patch.object(protocol, "verify_writer_processes", side_effect=RuntimeError("secret")), redirect_stdout(output):
            self.assertEqual(protocol.main([]), 1)
        self.assertNotIn("secret", output.getvalue())
