"""Real temporary SQLite transactions; mocked Git, Elasticsearch and Redis."""

import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import MagicMock, Mock, patch

os.environ.update(
    DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
    ELASTICSEARCH_URL="http://127.0.0.1:9200",
    REDIS_URL="redis://127.0.0.1:6379/15",
    HF_HUB_OFFLINE="1",
)

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import CodeFile, CodeSymbol, Repository, RepositorySyncJob
from app.indexer import incremental, repository
from app.indexer.symbols import index_repository_symbols
from app.search import engine as search_engine


class IndexerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine("sqlite://")
        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, autoflush=False, expire_on_commit=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        self.repo = Repository(name="demo", clone_url="https://github.com/owner/demo.git",
                               default_branch="main", last_indexed_commit="old")
        self.db.add(self.repo)
        self.db.flush()
        self.repo_id = self.repo.id
        self.file = CodeFile(repository_id=self.repo.id, path="sample.py", language="python")
        self.db.add(self.file)
        self.db.flush()
        self.db.add(CodeSymbol(repository_id=self.repo.id, file_id=self.file.id,
            name="old_function", qualified_name="old_function", kind="function",
            start_line=1, end_line=2, code="def old_function():\n    return 1"))
        self.db.commit()
        self.path = self.root / "owner" / "demo"
        self.path.mkdir(parents=True)
        (self.path / "sample.py").write_text("def new_function():\n    return 2\n")
        self.remote = "new"
        self.diff = "M\tsample.py"

        def git(path, *args):
            if args[0] == "rev-parse":
                return self.remote
            if args[0] == "diff":
                return self.diff
            return ""

        self.git = patch.object(incremental, "run_git", side_effect=git).start()
        patch.object(incremental, "REPOSITORY_ROOT", self.root).start()
        patch.object(repository, "REPOSITORY_ROOT", self.root).start()
        self.delete = patch.object(incremental, "delete_paths_from_elasticsearch").start()
        self.index = patch.object(incremental, "index_files_in_elasticsearch", return_value=1).start()
        self.invalidate = patch.object(incremental, "invalidate_search_cache", return_value=2).start()
        self.addCleanup(patch.stopall)

    def test_index_failure_keeps_checkpoint_retryable(self):
        self.index.side_effect = RuntimeError("Elasticsearch unavailable")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        with self.sessions() as verify:
            self.assertEqual(verify.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.index.side_effect = None
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertTrue(result["changed"])
        self.assertEqual(self.index.call_count, 2)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "new")

    def test_ingestion_does_not_refresh_after_commit(self):
        url = "https://github.com/owner/another.git"

        def clone(args, **kwargs):
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "main.py").write_text("def run():\n    return 1\n")

        with patch.object(repository.subprocess, "run", side_effect=clone), \
             patch.object(repository, "get_current_commit", return_value="initial"), \
             patch.object(repository, "get_current_branch", return_value="main"), \
             patch.object(self.db, "refresh", side_effect=RuntimeError("connection lost after commit")):
            result = repository.ingest_repository(self.db, url)
        self.assertTrue(Path(result["local_path"]).exists())
        self.assertIsNotNone(self.db.get(Repository, result["repository_id"]))

    def test_pending_target_survives_new_session_and_remote_advance(self):
        self.index.side_effect = RuntimeError("partial bulk failure")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        self.db.close()
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        job = self.db.get(RepositorySyncJob, self.repo_id)
        self.assertEqual(job.target_commit, "new")
        ids = [s.id for s in self.db.query(CodeSymbol).all()]
        self.remote = "newer"
        self.git.reset_mock()
        self.index.side_effect = None
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertTrue(result["resumed"])
        self.assertEqual(result["new_commit"], "new")
        self.git.assert_not_called()
        self.assertEqual(ids, [s.id for s in self.db.query(CodeSymbol).all()])
        self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))

    def test_full_indexing_refuses_to_overwrite_pending_metadata(self):
        self.index.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        symbols_before = [(s.id, s.name) for s in self.db.query(CodeSymbol)]
        with self.assertRaisesRegex(RuntimeError, "pending"):
            index_repository_symbols(self.db, self.repo_id)
        with patch.object(search_engine, "create_symbol_index") as create:
            with self.assertRaisesRegex(RuntimeError, "pending"):
                search_engine.index_repository_in_elasticsearch(self.db, self.repo_id)
        create.assert_not_called()
        self.assertEqual(symbols_before, [(s.id, s.name) for s in self.db.query(CodeSymbol)])

    def test_delete_failure_preserves_job_and_retries_deletion(self):
        self.delete.side_effect = RuntimeError("delete failed")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        self.index.assert_not_called()
        self.assertIsNotNone(self.db.get(RepositorySyncJob, self.repo_id))
        self.delete.side_effect = None
        self.assertTrue(incremental.sync_repository(self.db, self.repo_id)["resumed"])
        self.assertEqual(self.delete.call_count, 2)

    def test_cache_failure_keeps_job_and_old_checkpoint(self):
        self.invalidate.side_effect = ConnectionError("Redis unavailable")
        with self.assertRaises(ConnectionError):
            incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.assertIsNotNone(self.db.get(RepositorySyncJob, self.repo_id))
        self.invalidate.assert_called_once_with(strict=True)
        self.invalidate.side_effect = None
        self.assertTrue(incremental.sync_repository(self.db, self.repo_id)["resumed"])

    def test_preparation_commit_failure_rolls_back_without_search_mutations(self):
        with patch.object(self.db, "commit", side_effect=RuntimeError("commit failed")):
            with self.assertRaises(RuntimeError):
                incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.assertEqual(self.db.query(CodeSymbol).one().name, "old_function")
        self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))
        self.delete.assert_not_called()
        self.index.assert_not_called()

    def test_final_commit_failure_is_retryable(self):
        commit = self.db.commit
        count = 0

        def fail_final():
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("final commit failed")
            commit()

        with patch.object(self.db, "commit", side_effect=fail_final):
            with self.assertRaises(RuntimeError):
                incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.assertIsNotNone(self.db.get(RepositorySyncJob, self.repo_id))
        self.assertTrue(incremental.sync_repository(self.db, self.repo_id)["resumed"])

    def test_rename_delete_and_add_publish_correct_paths_and_symbols(self):
        self.diff = "R100\tsample.py\trenamed.py\nA\tadded.py"
        (self.path / "renamed.py").write_text("def renamed():\n    pass\n")
        (self.path / "added.py").write_text("def added():\n    pass\n")
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(result["files_renamed"], 1)
        self.assertEqual(result["files_added"], 1)
        self.assertEqual({f.path for f in self.db.query(CodeFile)}, {"renamed.py", "added.py"})
        self.assertEqual({s.name for s in self.db.query(CodeSymbol)}, {"renamed", "added"})
        self.delete.assert_called_once_with(repository_id=self.repo_id,
            paths=["added.py", "renamed.py", "sample.py"])

    def test_delete_only_publishes_even_without_new_symbols(self):
        self.diff = "D\tsample.py"
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(result["files_deleted"], 1)
        self.assertEqual(self.db.query(CodeFile).count(), 0)
        self.assertEqual(self.db.query(CodeSymbol).count(), 0)
        self.index.assert_called_once_with(db=self.db, file_ids=[])

    def test_unchanged_skips_publication(self):
        self.remote = "old"
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertFalse(result["changed"])
        self.delete.assert_not_called()
        self.index.assert_not_called()
        self.invalidate.assert_not_called()

    def test_parse_failure_rolls_back_symbols(self):
        with patch.object(incremental, "parse_python_source", side_effect=RuntimeError("parse failed")):
            with self.assertRaises(RuntimeError):
                incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.query(CodeSymbol).one().name, "old_function")
        self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))
        self.delete.assert_not_called()

    def test_git_failure_does_not_change_metadata(self):
        self.git.side_effect = subprocess.CalledProcessError(1, ["git", "fetch"])
        with self.assertRaises(subprocess.CalledProcessError):
            incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))

    def test_failed_clone_cleans_only_its_reserved_directory(self):
        with patch.object(repository.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["git"], stderr="clone failed")):
            with self.assertRaises(RuntimeError):
                repository.ingest_repository(self.db, "https://github.com/owner/broken.git")
        self.assertFalse((self.root / "owner" / "broken").exists())
        self.assertTrue(self.path.exists())
        self.assertEqual(self.db.query(Repository).count(), 1)

    def test_existing_clone_is_never_removed(self):
        with self.assertRaises(ValueError):
            repository.ingest_repository(self.db, "https://github.com/owner/demo")
        self.assertTrue((self.path / "sample.py").exists())

    def test_concurrent_directory_reservation_does_not_remove_winner(self):
        target = self.root / "owner" / "raced"
        mkdir = Path.mkdir

        def another_request_wins(path, *args, **kwargs):
            if path == target:
                mkdir(path)
                (path / "owned.txt").write_text("other request")
                raise FileExistsError(str(path))
            return mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", new=another_request_wins), \
             patch.object(repository.subprocess, "run") as clone:
            with self.assertRaises(ValueError):
                repository.ingest_repository(self.db, "https://github.com/owner/raced.git")
        clone.assert_not_called()
        self.assertEqual((target / "owned.txt").read_text(), "other request")

    def test_unsafe_repository_path_components_rejected(self):
        for url in ["https://github.com/../demo", "https://github.com/owner/..", "https://github.com/owner/..git"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                repository.parse_github_url(url)

    def test_lost_commit_acknowledgement_preserves_clone(self):
        commit = self.db.commit

        def committed_but_lost():
            commit()
            raise RuntimeError("lost commit acknowledgement")

        with patch.object(repository.subprocess, "run"), \
             patch.object(repository, "get_current_commit", return_value="initial"), \
             patch.object(repository, "get_current_branch", return_value="main"), \
             patch.object(self.db, "commit", side_effect=committed_but_lost):
            with self.assertRaises(RuntimeError):
                repository.ingest_repository(self.db, "https://github.com/owner/ack.git")
        self.assertTrue((self.root / "owner" / "ack").exists())
        self.assertEqual(self.db.query(Repository).filter_by(name="ack").count(), 1)


class SyncLockTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.bind = self.db.get_bind.return_value
        self.bind.dialect.name = "postgresql"
        self.connection = self.bind.connect.return_value.__enter__.return_value = Mock()
        self.connection.execute.return_value.scalar_one.return_value = True

    def test_lock_released_after_exception(self):
        with self.assertRaises(RuntimeError):
            with incremental.repository_sync_lock(self.db, 1):
                raise RuntimeError("work failed")
        queries = [str(call.args[0]) for call in self.connection.execute.call_args_list]
        self.assertIn("pg_try_advisory_lock", queries[0])
        self.assertIn("pg_advisory_unlock", queries[1])

    def test_busy_lock_rejected_without_unlocking_another_owner(self):
        self.connection.execute.return_value.scalar_one.return_value = False
        with self.assertRaises(incremental.RepositorySyncInProgress):
            with incremental.repository_sync_lock(self.db, 1):
                self.fail("must not run")
        self.assertEqual(self.connection.execute.call_count, 1)

    def test_unlock_failure_discards_connection(self):
        self.connection.execute.side_effect = [Mock(scalar_one=lambda: True), RuntimeError("connection lost")]
        with self.assertRaises(RuntimeError):
            with incremental.repository_sync_lock(self.db, 1):
                pass
        self.connection.invalidate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
