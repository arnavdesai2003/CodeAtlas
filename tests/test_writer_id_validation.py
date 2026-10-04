"""Repository IDs must not collide with corpus coordination keys."""
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

from app.indexer import locking


class WriterIdValidationTests(unittest.TestCase):
    def test_invalid_ids_never_acquire_locks_or_read_metadata(self):
        db = Mock()
        with patch.object(locking, "_writer_locks") as locks:
            for publication in (False, True):
                for repository_id in (-2147483648, -1, 0, 2147483648, True, False, 1.0, "1", None):
                    with self.subTest(repository_id=repository_id, publication=publication):
                        with self.assertRaisesRegex(ValueError, "positive int32"):
                            with locking.repository_sync_lock(db, repository_id, publication=publication):
                                self.fail("Invalid ID entered the writer context.")
            locks.assert_not_called()
        db.get.assert_not_called()
        db.get_bind.assert_not_called()

    def test_valid_boundary_ids_preserve_corpus_and_repository_keys(self):
        @contextmanager
        def acquired(*args):
            yield
        db = Mock()
        db.get.return_value = None
        with patch.object(locking, "_writer_locks", side_effect=acquired) as locks:
            for publication in (False, True):
                for repository_id in (1, locking.MAX_REPOSITORY_ID):
                    with locking.repository_sync_lock(db, repository_id, publication=publication):
                        pass
                    locks.assert_called_with(db, ((-1, "" if publication else "_shared"),
                                                  (repository_id, "")))
