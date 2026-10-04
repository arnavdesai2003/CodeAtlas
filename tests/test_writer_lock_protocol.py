"""Offline guard checks for the live PostgreSQL probe."""
import io
import os
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest.mock import Mock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1/test")
from scripts import verify_writer_locks as protocol


class WriterLockProtocolTests(unittest.TestCase):
    def test_non_postgres_is_rejected_before_queries(self):
        db = Mock()
        db.get_bind.return_value.dialect.name = "sqlite"
        with self.assertRaisesRegex(RuntimeError, "requires PostgreSQL"):
            protocol._preflight(db, (10, 20))
        db.execute.assert_not_called()

    def test_each_pending_job_blocks_probe(self):
        for position in range(3):
            db = Mock()
            db.get_bind.return_value.dialect.name = "postgresql"
            responses = [Mock() for _ in range(position + 1)]
            for response in responses[:-1]:
                response.first.return_value = None
            responses[-1].first.return_value = (object(),)
            db.execute.side_effect = responses
            with self.assertRaisesRegex(RuntimeError, "Pending writer"):
                protocol._preflight(db, (10, 20))

    def test_registered_probe_id_is_rejected(self):
        db = Mock()
        db.get_bind.return_value.dialect.name = "postgresql"
        responses = [Mock() for _ in range(4)]
        for response in responses[:3]:
            response.first.return_value = None
        responses[3].first.return_value = (10,)
        db.execute.side_effect = responses
        with self.assertRaisesRegex(RuntimeError, "registered"):
            protocol._preflight(db, (10, 20))

    def test_unexpected_backend_error_is_not_contention(self):
        @contextmanager
        def broken():
            raise ConnectionError("private backend detail")
            yield
        with self.assertRaises(ConnectionError):
            protocol._blocked(broken())

    def test_unexpected_acquisition_fails(self):
        @contextmanager
        def acquired():
            yield
        with self.assertRaises(AssertionError):
            protocol._blocked(acquired())

    def test_preflight_failure_closes_all_sessions(self):
        sessions = [Mock() for _ in range(3)]
        for session in sessions:
            session.__enter__ = Mock(return_value=session)
            session.__exit__ = Mock(return_value=False)
        with patch.object(protocol, "_preflight", side_effect=RuntimeError("pending")):
            with self.assertRaises(RuntimeError):
                protocol.verify_writer_locks(Mock(side_effect=sessions))
        for session in sessions:
            session.__exit__.assert_called_once()

    def test_cli_failure_is_nonzero_and_sanitized(self):
        output = io.StringIO()
        with patch.object(protocol, "verify_writer_locks", side_effect=RuntimeError("secret")), redirect_stdout(output):
            self.assertEqual(protocol.main(), 1)
        self.assertNotIn("secret", output.getvalue())
        self.assertIn('"status": "failed"', output.getvalue())
