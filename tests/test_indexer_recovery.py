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
from app.db.models import (
    CodeFile, CodeSymbol, Repository, RepositorySyncJob, RepositoryFullIndexJob,
)
from app.indexer import incremental, repository
from app.indexer.symbols import index_repository_symbols
from app.search import engine as search_engine, publication
from elasticsearch import NotFoundError
from elastic_transport import ApiResponseMeta, NodeConfig


class IndexerRecoveryTests(unittest.TestCase):
    def test_incomplete_incremental_bulk_keeps_checkpoint_and_replays_ids(self):
        self.index.side_effect = lambda **kwargs: search_engine.index_files_in_elasticsearch(**kwargs)
        with patch.object(search_engine, "resolve_search_index", return_value="fixed"), \
             patch.object(search_engine, "create_symbol_index"), \
             patch.object(search_engine, "elasticsearch_client") as es, \
             patch.object(search_engine, "embed_texts", return_value=[[0.0] * 384]), \
             patch.object(search_engine, "bulk") as bulk:
            committed_ids = None
            for outcome in ((0, []), (1, [{"index": {"status": 500}}]), (2, [])):
                bulk.return_value = outcome
                with self.subTest(outcome=outcome), self.assertRaisesRegex(RuntimeError, "bulk indexing was incomplete"):
                    incremental.sync_repository(self.db, self.repo_id)
                job = self.db.get(RepositorySyncJob, self.repo_id)
                self.assertIsNotNone(job)
                ids = [row.id for row in self.db.query(CodeSymbol)]
                if committed_ids is None:
                    committed_ids = ids
                self.assertEqual(ids, committed_ids)
                self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
                self.invalidate.assert_not_called()
                es.indices.refresh.assert_not_called()
            bulk.return_value = (1, [])
            with patch.object(incremental, "parse_python_source") as parse:
                result = incremental.sync_repository(self.db, self.repo_id)
            parse.assert_not_called()
            self.assertTrue(result["resumed"])
            self.assertEqual(result["symbols_indexed"], 1)
            self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))
            self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "new")
            es.indices.refresh.assert_called_once()
            self.invalidate.assert_called_once_with(strict=True)

    def test_invalid_source_encoding_rolls_back_sync_and_can_retry(self):
        (self.path / "sample.py").write_bytes(b'def corrupt(): return "\xff"\n')
        with self.assertRaises((SyntaxError, UnicodeError)):
            incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.query(CodeSymbol).one().name, "old_function")
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))
        self.index.assert_not_called()
        (self.path / "sample.py").write_text("def repaired(): pass\n")
        incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.query(CodeSymbol).one().name, "repaired")

    def test_invalid_source_encoding_rolls_back_full_symbol_replacement(self):
        (self.path / "sample.py").write_bytes(b'def corrupt(): return "\xff"\n')
        with patch("app.indexer.symbols.REPOSITORY_ROOT", self.root):
            with self.assertRaises((SyntaxError, UnicodeError)):
                index_repository_symbols(self.db, self.repo_id)
        self.assertEqual(self.db.query(CodeSymbol).one().name, "old_function")
        self.assertIsNone(self.db.get(RepositoryFullIndexJob, self.repo_id))
    def test_copy_publication_retry_preserves_source_identity_and_symbols(self):
        destination = "copy\tline\n.py"
        (self.path / destination).write_text("def copied(): pass\n")
        source_id = self.file.id
        source_symbol_id = self.db.query(CodeSymbol).one().id
        self.diff = "C100\0sample.py\0" + destination + "\0"
        self.index.side_effect = RuntimeError("ES unavailable")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        job = self.db.get(RepositorySyncJob, self.repo_id)
        self.assertEqual(job.affected_paths, [destination])
        self.assertEqual(job.stats["files_added"], 1)
        self.assertEqual(job.stats["files_renamed"], 0)
        self.assertEqual(self.db.query(CodeFile).filter_by(path="sample.py").one().id, source_id)
        self.assertEqual(self.db.get(CodeSymbol, source_symbol_id).name, "old_function")
        self.index.side_effect = None
        with patch.object(incremental, "parse_python_source") as parse:
            result = incremental.sync_repository(self.db, self.repo_id)
        parse.assert_not_called()
        self.assertTrue(result["resumed"])
        self.assertEqual(self.db.query(CodeFile).count(), 2)
        self.assertEqual({row.name for row in self.db.query(CodeSymbol)}, {"old_function", "copied"})
        self.assertEqual(self.delete.call_args.kwargs["paths"], [destination])

    def test_real_git_rename_retry_journals_both_exact_paths(self):
        from app.indexer.git import git_output, repository_git_output
        git_output("init", str(self.path))

        def commit():
            repository_git_output(self.path, "add", "-A")
            repository_git_output(self.path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                  "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
            return repository_git_output(self.path, "rev-parse", "HEAD")

        old = commit()
        self.repo.last_indexed_commit = old
        self.db.commit()
        destination = "renamed\tline\n.py"
        (self.path / "sample.py").rename(self.path / destination)
        target = commit()
        repository_git_output(self.path, "update-ref", "refs/remotes/origin/main", target)
        repository_git_output(self.path, "reset", "--hard", old)

        def real_git(path, *args):
            if args[0] == "fetch":
                return ""
            return repository_git_output(path, "-c", "diff.renames=true", *args)

        self.git.side_effect = real_git
        self.index.side_effect = RuntimeError("ES unavailable")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        job = self.db.get(RepositorySyncJob, self.repo_id)
        self.assertEqual(set(job.affected_paths), {"sample.py", destination})
        self.assertEqual(job.stats["files_renamed"], 1)
        self.assertEqual({row.path for row in self.db.query(CodeFile)}, {destination})
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, old)
        self.assertFalse((self.path / "sample.py").exists())
        self.assertTrue((self.path / destination).is_file())
        self.index.side_effect = None
        self.git.reset_mock()
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertTrue(result["resumed"])
        self.git.assert_not_called()
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, target)
        self.assertEqual(self.db.query(CodeSymbol).one().name, "new_function")

    def test_real_git_type_changes_and_publication_recovery(self):
        from app.indexer.git import git_output, repository_git_output
        git_output("init", str(self.path))

        def commit():
            repository_git_output(self.path, "add", "--", "sample.py")
            repository_git_output(self.path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                  "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
            return repository_git_output(self.path, "rev-parse", "HEAD")

        old = commit()
        self.repo.last_indexed_commit = old
        self.db.commit()
        outside = self.root / "private.py"
        outside.write_text("def private(): pass\n")
        (self.path / "sample.py").unlink()
        (self.path / "sample.py").symlink_to(outside)
        target = commit()
        repository_git_output(self.path, "update-ref", "refs/remotes/origin/main", target)
        repository_git_output(self.path, "reset", "--hard", old)

        def real_git(path, *args):
            # Only network fetch is replaced. Diff/ref/reset operate on real Git.
            return "" if args[0] == "fetch" else repository_git_output(path, *args)

        self.git.side_effect = real_git
        self.delete.side_effect = RuntimeError("ES unavailable")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        self.assertTrue((self.path / "sample.py").is_symlink())
        self.assertEqual(self.db.query(CodeSymbol).count(), 0)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, old)
        job = self.db.get(RepositorySyncJob, self.repo_id)
        self.assertEqual(job.affected_paths, ["sample.py"])
        self.delete.side_effect = None
        self.assertTrue(incremental.sync_repository(self.db, self.repo_id)["resumed"])
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, target)

        (self.path / "sample.py").unlink()
        (self.path / "sample.py").write_text("def restored(): pass\n")
        restored = commit()
        repository_git_output(self.path, "update-ref", "refs/remotes/origin/main", restored)
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(result["files_added"], 1)
        self.assertEqual(self.db.query(CodeSymbol).one().name, "restored")
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, restored)
        self.assertEqual(outside.read_text(), "def private(): pass\n")

    def test_type_change_with_existing_metadata_counts_as_modified(self):
        self.diff = "T\0sample.py\0"
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(result["files_modified"], 1)
        self.assertEqual(result["files_added"], 0)

    def test_unusual_diff_path_is_journaled_and_indexed_exactly(self):
        name = "tab\tline\n.py"
        (self.path / name).write_text("def unusual(): pass\n")
        self.diff = "A\0" + name + "\0"
        incremental.sync_repository(self.db, self.repo_id)
        self.assertIsNotNone(self.db.query(CodeFile).filter_by(path=name).first())
        self.assertIn(name, self.delete.call_args.kwargs["paths"])

    def test_malformed_diff_blocks_reset_and_checkpoint(self):
        self.diff = "M\0truncated"
        with self.assertRaises(ValueError):
            incremental.sync_repository(self.db, self.repo_id)
        self.assertFalse(any(call.args[1] == "reset" for call in self.git.call_args_list))
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.assertEqual(self.db.query(CodeSymbol).count(), 1)
        self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))

    def test_missing_git_metadata_blocks_new_sync_before_git_or_mutation(self):
        from app.indexer.errors import UnsafeClonePath
        (self.path / ".git").rmdir()
        with self.assertRaises(UnsafeClonePath):
            incremental.sync_repository(self.db, self.repo_id)
        self.git.assert_not_called()
        self.assertEqual(self.db.query(CodeSymbol).count(), 1)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")

    def test_pending_publication_resumes_without_git_metadata(self):
        self.index.side_effect = RuntimeError("ES unavailable")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        (self.path / ".git").rmdir()
        self.index.side_effect = None
        self.git.reset_mock()
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertTrue(result["resumed"])
        self.git.assert_not_called()

    def test_clone_timeout_cleans_only_reserved_uncommitted_clone(self):
        from app.indexer.errors import RepositoryCloneFailed
        with patch.object(repository.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 120)):
            with self.assertRaisesRegex(RepositoryCloneFailed, "timed out"):
                repository.ingest_repository(self.db, "https://github.com/owner/timedout.git")
        self.assertFalse((self.root / "owner/timedout").exists())
        self.assertTrue((self.path / "sample.py").exists())
        self.assertEqual(self.db.query(Repository).count(), 1)

    def test_sync_git_timeout_preserves_checkpoint_and_metadata(self):
        self.git.side_effect = subprocess.TimeoutExpired("git", 120)
        with self.assertRaises(subprocess.TimeoutExpired):
            incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.assertEqual(self.db.query(CodeSymbol).count(), 1)
        self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))
        self.index.assert_not_called()
        self.invalidate.assert_not_called()

    def test_redirected_clone_blocks_sync_and_full_before_reads(self):
        redirected = self.root / "redirected"
        self.path.rename(redirected)
        self.path.symlink_to(redirected, target_is_directory=True)
        from app.indexer.errors import UnsafeClonePath
        with self.assertRaises(UnsafeClonePath):
            incremental.sync_repository(self.db, self.repo_id)
        self.git.assert_not_called()
        with patch("app.indexer.symbols.REPOSITORY_ROOT", self.root), \
             patch("app.indexer.symbols.parse_python_source") as parse:
            with self.assertRaises(UnsafeClonePath):
                index_repository_symbols(self.db, self.repo_id)
        parse.assert_not_called()
        self.assertEqual(self.db.query(CodeSymbol).count(), 1)

    def test_redirected_owner_blocks_ingestion_without_git_or_cleanup(self):
        from app.indexer.errors import UnsafeClonePath
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (self.root / "redirect").symlink_to(outside, target_is_directory=True)
        with patch.object(repository.subprocess, "run") as git:
            with self.assertRaises(UnsafeClonePath):
                repository.ingest_repository(self.db, "https://github.com/redirect/new.git")
        git.assert_not_called()
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertFalse((outside / "new").exists())

    def test_sync_regular_file_to_symlink_removes_stale_metadata_and_symbols(self):
        outside = self.root / "private.py"
        outside.write_text("def secret(): pass\n")
        (self.path / "sample.py").unlink()
        (self.path / "sample.py").symlink_to(outside)
        with patch.object(incremental, "calculate_file_hash") as hashed, \
             patch.object(incremental, "parse_python_source") as parsed:
            incremental.sync_repository(self.db, self.repo_id)
        hashed.assert_not_called()
        parsed.assert_not_called()
        self.assertEqual(self.db.query(CodeFile).count(), 0)
        self.assertEqual(self.db.query(CodeSymbol).count(), 0)
        self.assertIn("sample.py", self.delete.call_args.kwargs["paths"])
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "new")

    def test_full_symbols_skip_symlinks_and_clear_old_symbols(self):
        outside = self.root / "private.py"
        outside.write_text("def secret(): pass\n")
        (self.path / "sample.py").unlink()
        (self.path / "sample.py").symlink_to(outside)
        with patch("app.indexer.symbols.REPOSITORY_ROOT", self.root), \
             patch("app.indexer.symbols.parse_python_source") as parsed:
            result = index_repository_symbols(self.db, self.repo_id)
        parsed.assert_not_called()
        self.assertEqual(result["skipped_files"], 1)
        self.assertEqual(self.db.query(CodeSymbol).count(), 0)

    def test_symlink_replacement_deletion_failure_is_replayable(self):
        (self.path / "sample.py").unlink()
        (self.path / "sample.py").symlink_to(self.root / "absent.py")
        self.delete.side_effect = RuntimeError("ES unavailable")
        with self.assertRaises(RuntimeError):
            incremental.sync_repository(self.db, self.repo_id)
        self.assertEqual(self.db.get(Repository, self.repo_id).last_indexed_commit, "old")
        self.assertEqual(self.db.query(CodeSymbol).count(), 0)
        self.assertIsNotNone(self.db.get(RepositorySyncJob, self.repo_id))
        self.delete.side_effect = None
        result = incremental.sync_repository(self.db, self.repo_id)
        self.assertTrue(result["resumed"])
        self.assertIsNone(self.db.get(RepositorySyncJob, self.repo_id))

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
        (self.path / ".git").mkdir()
        (self.path / "sample.py").write_text("def new_function():\n    return 2\n")
        self.remote = "new"
        self.diff = "M\tsample.py"

        def git(path, *args):
            if args[0] == "rev-parse":
                return self.remote
            if args[0] == "diff":
                return self.diff if "\0" in self.diff or not self.diff else self.diff.replace("\t", "\0").replace("\n", "\0") + "\0"
            return ""

        self.git = patch.object(incremental, "run_git", side_effect=git).start()
        patch.object(incremental, "REPOSITORY_ROOT", self.root).start()
        patch.object(repository, "REPOSITORY_ROOT", self.root).start()
        self.delete = patch.object(incremental, "delete_paths_from_elasticsearch").start()
        self.index = patch.object(incremental, "index_files_in_elasticsearch", return_value=1).start()
        self.invalidate = patch.object(incremental, "invalidate_search_cache", return_value=2).start()
        self.addCleanup(patch.stopall)

    def publication_es(self, es):
        es.options.return_value = es
        es.indices.get_mapping.side_effect = lambda **kw: {kw["index"]: {"mappings": {}}}
        es.indices.get_settings.side_effect = lambda **kw: {kw["index"]: {"settings": {"index": {"uuid": "test-uuid"}}}}
        self.active_index = None
        def get_alias(**kwargs):
            if self.active_index is None:
                raise NotFoundError("missing", meta=ApiResponseMeta(404, "1.1", {}, 0,
                    NodeConfig("http", "localhost", 9200)), body={})
            return {self.active_index: {"aliases": {search_engine.SEARCH_ALIAS: {}}}}
        def update_aliases(**kwargs):
            self.active_index = kwargs["actions"][-1]["add"]["index"]
            return {"acknowledged": True}
        es.indices.get_alias.side_effect = get_alias
        es.indices.update_aliases.side_effect = update_aliases
        es.reindex.return_value = {"total": 0, "created": 0}
        es.count.side_effect = lambda **kwargs: {"count": 0 if "query" in kwargs
            else self.db.query(CodeSymbol).count()}

    def prepare_full(self):
        with patch("app.indexer.symbols.REPOSITORY_ROOT", self.root):
            return index_repository_symbols(self.db, self.repo_id)

    def test_full_symbol_snapshot_is_durable_and_retry_does_not_reparse(self):
        result = self.prepare_full()
        self.assertTrue(result["publication_pending"])
        before = [(s.id, s.name) for s in self.db.query(CodeSymbol)]
        (self.path / "sample.py").write_text("def later(): pass")
        with self.sessions() as retry, patch("app.indexer.symbols.parse_python_source") as parse:
            result = index_repository_symbols(retry, self.repo_id)
            self.assertTrue(result["resumed"])
            parse.assert_not_called()
            self.assertEqual(before, [(s.id, s.name) for s in retry.query(CodeSymbol)])

    def test_full_symbol_parse_failure_rolls_back_entire_snapshot(self):
        with patch("app.indexer.symbols.REPOSITORY_ROOT", self.root), \
             patch("app.indexer.symbols.parse_python_source", side_effect=ValueError("parse")):
            with self.assertRaises(ValueError):
                index_repository_symbols(self.db, self.repo_id)
        self.assertEqual(self.db.query(CodeSymbol).one().name, "old_function")
        self.assertIsNone(self.db.get(RepositoryFullIndexJob, self.repo_id))

    def test_full_snapshot_clears_symbols_for_missing_files(self):
        (self.path / "sample.py").unlink()
        self.assertEqual(self.prepare_full()["symbols_indexed"], 0)
        self.assertEqual(self.db.query(CodeSymbol).count(), 0)
        self.assertIsNotNone(self.db.get(RepositoryFullIndexJob, self.repo_id))

    def test_sync_refuses_pending_full_publication_before_git(self):
        self.prepare_full()
        with self.assertRaisesRegex(RuntimeError, "pending full"):
            incremental.sync_repository(self.db, self.repo_id)
        self.git.assert_not_called()

    def test_full_publication_failure_retries_committed_snapshot_in_new_session(self):
        self.prepare_full()
        with patch.object(search_engine, "create_symbol_index"), \
             patch.object(search_engine, "elasticsearch_client") as es, \
             patch.object(search_engine, "_write_repository_index", side_effect=RuntimeError("bulk")) as write, \
             patch.object(publication, "invalidate_search_cache", return_value=0) as invalidate:
            self.publication_es(es)
            with self.assertRaises(RuntimeError):
                search_engine.index_repository_in_elasticsearch(self.db, self.repo_id)
            invalidate.assert_not_called()
            with self.sessions() as retry:
                self.assertIsNotNone(retry.get(RepositoryFullIndexJob, self.repo_id))
                write.side_effect = None
                write.return_value = {"symbols_indexed": 1}
                result = search_engine.index_repository_in_elasticsearch(retry, self.repo_id)
                self.assertTrue(result["resumed"])
                self.assertIsNone(retry.get(RepositoryFullIndexJob, self.repo_id))
                self.assertEqual(retry.get(Repository, self.repo_id).last_indexed_commit, "old")
            self.assertEqual(es.reindex.call_count, 2)
            self.assertEqual(es.reindex.call_args.kwargs["source"]["query"],
                {"bool": {"must_not": [{"term": {"repository_id": self.repo_id}}]}})
            invalidate.assert_called_once_with(strict=True)

    def test_full_publication_incomplete_copy_keeps_job_without_bulk(self):
        for response in ({"timed_out": True}, {"failures": ["error"]}, {"version_conflicts": 1}):
            with self.subTest(response=response), patch.object(search_engine, "create_symbol_index"), \
                 patch.object(search_engine, "elasticsearch_client") as es, \
                 patch.object(search_engine, "_write_repository_index") as write:
                self.publication_es(es)
                es.reindex.return_value = response
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    search_engine.index_repository_in_elasticsearch(self.db, self.repo_id)
                write.assert_not_called()
                self.assertIsNotNone(self.db.get(RepositoryFullIndexJob, self.repo_id))

    def test_full_publication_cache_and_final_commit_failures_keep_job(self):
        self.prepare_full()
        with patch.object(search_engine, "create_symbol_index"), \
             patch.object(search_engine, "elasticsearch_client") as es, \
             patch.object(search_engine, "_write_repository_index", return_value={"symbols_indexed": 1}), \
             patch.object(publication, "invalidate_search_cache", side_effect=RuntimeError("Redis")) as invalidate:
            self.publication_es(es)
            with self.assertRaises(RuntimeError):
                search_engine.index_repository_in_elasticsearch(self.db, self.repo_id)
            self.assertIsNotNone(self.db.get(RepositoryFullIndexJob, self.repo_id))
            invalidate.side_effect = None
            with patch.object(self.db, "commit", side_effect=RuntimeError("commit")):
                with self.assertRaises(RuntimeError):
                    search_engine.index_repository_in_elasticsearch(self.db, self.repo_id)
            with self.sessions() as verify:
                self.assertIsNotNone(verify.get(RepositoryFullIndexJob, self.repo_id))

    def test_full_preparation_commit_failure_preserves_old_symbols(self):
        with patch.object(self.db, "commit", side_effect=RuntimeError("commit")):
            with self.assertRaises(RuntimeError):
                self.prepare_full()
        self.assertEqual(self.db.query(CodeSymbol).one().name, "old_function")
        self.assertIsNone(self.db.get(RepositoryFullIndexJob, self.repo_id))

    def test_empty_full_snapshot_still_deletes_and_invalidates(self):
        (self.path / "sample.py").unlink()
        self.prepare_full()
        with patch.object(search_engine, "create_symbol_index"), \
             patch.object(search_engine, "elasticsearch_client") as es, \
             patch.object(search_engine, "embed_texts") as embed, \
             patch.object(publication, "invalidate_search_cache", return_value=0) as invalidate:
            self.publication_es(es)
            result = search_engine.index_repository_in_elasticsearch(self.db, self.repo_id)
            self.assertEqual(result["symbols_indexed"], 0)
            es.reindex.assert_called_once()
            embed.assert_not_called()
            invalidate.assert_called_once_with(strict=True)
            self.assertIsNone(self.db.get(RepositoryFullIndexJob, self.repo_id))

    def test_incomplete_full_bulk_keeps_job_without_invalidating(self):
        self.prepare_full()
        with patch.object(search_engine, "create_symbol_index"), \
             patch.object(search_engine, "elasticsearch_client") as es, \
             patch.object(search_engine, "embed_texts", return_value=[[0.0] * 384]), \
             patch.object(search_engine, "bulk", return_value=(0, [])), \
             patch.object(publication, "invalidate_search_cache") as invalidate:
            self.publication_es(es)
            with self.assertRaisesRegex(RuntimeError, "bulk indexing was incomplete"):
                search_engine.index_repository_in_elasticsearch(self.db, self.repo_id)
            invalidate.assert_not_called()
            self.assertIsNotNone(self.db.get(RepositoryFullIndexJob, self.repo_id))

    def test_standalone_full_job_blocks_symbol_replacement(self):
        self.db.add(RepositoryFullIndexJob(repository_id=self.repo_id, stats={}))
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, "pending full"):
            self.prepare_full()
        self.assertEqual(self.db.query(CodeSymbol).one().name, "old_function")

    def test_full_writers_acquire_shared_lock_before_preflight(self):
        from app.indexer import symbols
        for module, operation in ((symbols, symbols.index_repository_symbols),
                                  (search_engine, search_engine.index_repository_in_elasticsearch)):
            with self.subTest(operation=operation.__name__), \
                 patch.object(module, "repository_sync_lock", side_effect=incremental.RepositorySyncInProgress("busy")), \
                 patch.object(self.db, "get") as get:
                with self.assertRaises(incremental.RepositorySyncInProgress):
                    operation(self.db, self.repo_id)
                get.assert_not_called()

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
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

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

    def test_lost_preparation_ack_replays_committed_snapshot_in_new_session(self):
        commit = self.db.commit

        def committed_but_lost():
            commit()
            raise RuntimeError("lost preparation acknowledgement")

        with patch.object(self.db, "commit", side_effect=committed_but_lost):
            with self.assertRaisesRegex(RuntimeError, "lost preparation"):
                incremental.sync_repository(self.db, self.repo_id)
        self.delete.assert_not_called()
        self.index.assert_not_called()
        self.invalidate.assert_not_called()
        self.db.close()
        self.remote = "newer"
        (self.path / "sample.py").write_text("def newer_function():\n    pass\n")
        with self.sessions() as retry:
            job = retry.get(RepositorySyncJob, self.repo_id)
            self.assertEqual(job.target_commit, "new")
            self.assertEqual(retry.get(Repository, self.repo_id).last_indexed_commit, "old")
            committed_ids = [symbol.id for symbol in retry.query(CodeSymbol).all()]
            self.assertEqual(retry.query(CodeSymbol).one().name, "new_function")
            self.git.reset_mock()
            with patch.object(incremental, "parse_python_source", side_effect=AssertionError("must not reparse")):
                result = incremental.sync_repository(retry, self.repo_id)
            self.git.assert_not_called()
            self.assertTrue(result["resumed"])
            self.assertEqual(result["new_commit"], "new")
            self.assertEqual([symbol.id for symbol in retry.query(CodeSymbol).all()], committed_ids)
            self.assertIsNone(retry.get(RepositorySyncJob, self.repo_id))
            self.assertEqual(retry.get(Repository, self.repo_id).last_indexed_commit, "new")
        self.delete.assert_called_once_with(repository_id=self.repo_id, paths=["sample.py"])
        self.invalidate.assert_called_once_with(strict=True)

    def test_lost_final_ack_is_already_complete_in_new_session(self):
        commit = self.db.commit
        count = 0

        def lose_final_ack():
            nonlocal count
            count += 1
            commit()
            if count == 2:
                raise RuntimeError("lost final acknowledgement")

        with patch.object(self.db, "commit", side_effect=lose_final_ack):
            with self.assertRaisesRegex(RuntimeError, "lost final"):
                incremental.sync_repository(self.db, self.repo_id)
        self.db.close()
        with self.sessions() as retry:
            self.assertIsNone(retry.get(RepositorySyncJob, self.repo_id))
            self.assertEqual(retry.get(Repository, self.repo_id).last_indexed_commit, "new")
            committed_ids = [symbol.id for symbol in retry.query(CodeSymbol).all()]
            with patch.object(incremental, "parse_python_source", side_effect=AssertionError("must not reparse")):
                result = incremental.sync_repository(retry, self.repo_id)
            self.assertFalse(result["changed"])
            self.assertEqual(result["new_commit"], "new")
            self.assertEqual([symbol.id for symbol in retry.query(CodeSymbol).all()], committed_ids)
        self.delete.assert_called_once()
        self.index.assert_called_once()
        self.invalidate.assert_called_once_with(strict=True)

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
        self.db.get.return_value = None
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
        self.assertIn("pg_try_advisory_lock_shared", queries[0])
        self.assertIn("pg_try_advisory_lock", queries[1])
        self.assertIn("pg_advisory_unlock", queries[2])
        self.assertIn("pg_advisory_unlock_shared", queries[3])

    def test_busy_lock_rejected_without_unlocking_another_owner(self):
        self.connection.execute.return_value.scalar_one.return_value = False
        with self.assertRaises(incremental.RepositorySyncInProgress):
            with incremental.repository_sync_lock(self.db, 1):
                self.fail("must not run")
        self.assertEqual(self.connection.execute.call_count, 1)

    def test_unlock_failure_discards_connection(self):
        self.connection.execute.side_effect = [Mock(scalar_one=lambda: True), Mock(scalar_one=lambda: True), RuntimeError("connection lost")]
        with self.assertRaises(RuntimeError):
            with incremental.repository_sync_lock(self.db, 1):
                pass
        self.connection.invalidate.assert_called_once()

    def test_busy_repository_lock_releases_acquired_shared_corpus_lock(self):
        self.connection.execute.side_effect = [Mock(scalar_one=lambda: True),
            Mock(scalar_one=lambda: False), Mock(scalar_one=lambda: True)]
        with self.assertRaises(incremental.RepositorySyncInProgress):
            with incremental.repository_sync_lock(self.db, 1):
                self.fail("must not acquire repository")
        queries = [str(call.args[0]) for call in self.connection.execute.call_args_list]
        self.assertEqual(len(queries), 3)
        self.assertIn("pg_advisory_unlock_shared", queries[-1])

    def test_publication_acquires_exclusive_corpus_lock(self):
        with incremental.repository_sync_lock(self.db, 1, publication=True):
            pass
        queries = [str(call.args[0]) for call in self.connection.execute.call_args_list]
        self.assertNotIn("shared", queries[0])
        self.assertEqual(self.connection.execute.call_args_list[0].args[1]["repository_id"], -1)
        self.assertEqual(len(queries), 4)

    def test_lost_lock_invalidates_connection(self):
        self.connection.execute.side_effect = [Mock(scalar_one=lambda: True),
            Mock(scalar_one=lambda: True), Mock(scalar_one=lambda: False)]
        with self.assertRaisesRegex(RuntimeError, "lock was lost"):
            with incremental.repository_sync_lock(self.db, 1):
                pass
        self.connection.invalidate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
