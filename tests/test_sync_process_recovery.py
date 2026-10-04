"""Abrupt process-exit recovery with disk SQLite and mocked external services."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_support import _sessions, run_exit_child

from app.db.database import Base
from app.db.models import CodeFile, CodeSymbol, Repository, RepositorySyncJob
from app.indexer import incremental


def _exit_during_sync(database_path, root, boundary):
    engine, sessions = _sessions(database_path)
    def git(path, *args):
        if args[0] == "rev-parse":
            return "new"
        if args[0] == "diff":
            return "M\0sample.py\0"
        return ""

    def external(stage, result):
        def execute(*args, **kwargs):
            if boundary == stage:
                os._exit(73)
            return result
        return execute

    with sessions() as db, \
         patch.object(incremental, "REPOSITORY_ROOT", Path(root)), \
         patch.object(incremental, "validate_git_metadata"), \
         patch.object(incremental, "run_git", side_effect=git), \
         patch.object(incremental, "delete_paths_from_elasticsearch", side_effect=external("delete", 1)), \
         patch.object(incremental, "index_files_in_elasticsearch", side_effect=external("index", 1)), \
         patch.object(incremental, "invalidate_search_cache", side_effect=external("cache", 1)):
        commit = db.commit
        commits = 0
        def interrupted_commit():
            nonlocal commits
            commits += 1
            if commits == 1 and boundary == "before_metadata_commit":
                os._exit(73)
            if commits == 2 and boundary == "before_final_commit":
                os._exit(73)
            commit()
            if commits == 2 and boundary == "after_final_commit":
                os._exit(73)
        with patch.object(db, "commit", side_effect=interrupted_commit):
            incremental.sync_repository(db, 1)
    engine.dispose()
    raise AssertionError("Expected exit boundary was not reached.")


class SyncProcessRecoveryTests(unittest.TestCase):
    def exercise_boundary(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clone = root / "owner" / "fixture"
            clone.mkdir(parents=True)
            (clone / "sample.py").write_text("def recovered_target():\n    return 2\n")
            database = root / "metadata.sqlite"
            engine, sessions = _sessions(database)
            try:
                Base.metadata.create_all(engine)
                with sessions() as db:
                    db.add(Repository(id=1, name="fixture", clone_url="https://github.com/owner/fixture",
                                      default_branch="main", last_indexed_commit="old"))
                    db.flush()
                    db.add(CodeFile(id=1, repository_id=1, path="sample.py", language="python"))
                    db.flush()
                    db.add(CodeSymbol(repository_id=1, file_id=1, name="original",
                                      qualified_name="original", kind="function", start_line=1,
                                      end_line=2, code="def original():\n    return 1"))
                    db.commit()
                run_exit_child(_exit_during_sync, (str(database), str(root), boundary), exitcode=73)
                with sessions() as db:
                    symbol = db.query(CodeSymbol).one()
                    if boundary == "before_metadata_commit":
                        self.assertEqual(symbol.name, "original")
                        self.assertEqual(db.get(Repository, 1).last_indexed_commit, "old")
                        self.assertIsNone(db.get(RepositorySyncJob, 1))
                        return
                    self.assertEqual(symbol.name, "recovered_target")
                    symbol_id = symbol.id
                    pending = db.get(RepositorySyncJob, 1)
                    repository = db.get(Repository, 1)
                    if boundary == "after_final_commit":
                        self.assertIsNone(pending)
                        self.assertEqual(repository.last_indexed_commit, "new")
                        return
                    self.assertEqual(repository.last_indexed_commit, "old")
                    self.assertIsNotNone(pending)
                    self.assertEqual(pending.target_commit, "new")
                    self.assertEqual(pending.file_ids, [symbol.file_id])
                    self.assertEqual(pending.affected_paths, ["sample.py"])
                    with patch.object(incremental, "run_git", side_effect=AssertionError("Recovery fetched Git")), \
                         patch.object(incremental, "parse_python_source", side_effect=AssertionError("Recovery reparsed")), \
                         patch.object(incremental, "delete_paths_from_elasticsearch", return_value=1) as delete, \
                         patch.object(incremental, "index_files_in_elasticsearch", return_value=1) as index, \
                         patch.object(incremental, "invalidate_search_cache", return_value=1) as invalidate:
                        result = incremental.sync_repository(db, 1)
                    self.assertTrue(result["resumed"])
                    self.assertEqual(result["new_commit"], "new")
                    delete.assert_called_once_with(repository_id=1, paths=["sample.py"])
                    index.assert_called_once_with(db=db, file_ids=[symbol.file_id])
                    invalidate.assert_called_once_with(strict=True)
                with sessions() as verify:
                    self.assertEqual(verify.query(CodeSymbol).one().id, symbol_id)
                    self.assertEqual(verify.get(Repository, 1).last_indexed_commit, "new")
                    self.assertIsNone(verify.get(RepositorySyncJob, 1))
            finally:
                engine.dispose()

    def test_exit_before_external_deletion(self):
        self.exercise_boundary("delete")

    def test_exit_before_metadata_commit(self):
        self.exercise_boundary("before_metadata_commit")

    def test_exit_before_external_indexing(self):
        self.exercise_boundary("index")

    def test_exit_before_cache_rotation(self):
        self.exercise_boundary("cache")

    def test_exit_before_final_commit(self):
        self.exercise_boundary("before_final_commit")

    def test_exit_after_final_commit(self):
        self.exercise_boundary("after_final_commit")
