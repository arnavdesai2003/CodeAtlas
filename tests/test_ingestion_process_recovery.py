"""Ingestion exits preserve orphan/registered clones for manual reconciliation."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.db.database import Base
from app.db.models import CodeFile, CodeSymbol, Repository
from app.indexer import repository
from app.indexer.errors import RepositoryConflict
from recovery_support import _sessions, run_exit_child, URL, SOURCE

def _exit_during_ingestion(database, root, boundary):
    sql, sessions = _sessions(database)
    def stop(point):
        if boundary == point:
            os._exit(75)
    def clone(*args):
        stop("reserved")
        destination = Path(args[-1])
        (destination / "sample.py").write_text(SOURCE)
        (destination / "notes.txt").write_text("retained fixture")
        stop("cloned")
    discover = repository.discover_source_files
    def discovery(path):
        files = discover(path)
        stop("discovered")
        return files
    try:
        with sessions() as db, \
             patch.object(repository, "REPOSITORY_ROOT", Path(root)), \
             patch.object(repository, "git_output", side_effect=clone), \
             patch.object(repository, "get_current_commit", return_value="fixture_commit"), \
             patch.object(repository, "get_current_branch", return_value="main"), \
             patch.object(repository, "discover_source_files", side_effect=discovery):
            commit = db.commit
            def interrupted_commit():
                stop("before_commit")
                commit()
                stop("after_commit")
            with patch.object(db, "commit", side_effect=interrupted_commit):
                repository.ingest_repository(db, URL)
        raise AssertionError("Expected ingestion exit boundary was not reached.")
    finally:
        sql.dispose()


class IngestionProcessRecoveryTests(unittest.TestCase):
    def exercise_boundary(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "metadata.sqlite"
            clones = root / "clones"
            destination = clones / "owner" / "fixture"
            sql, sessions = _sessions(database)
            try:
                Base.metadata.create_all(sql)
                run_exit_child(_exit_during_ingestion, (str(database), str(clones), boundary), exitcode=75)
                self.assertTrue(destination.is_dir())
                before = {path.relative_to(destination).as_posix(): path.read_bytes()
                          for path in destination.rglob("*") if path.is_file()}
                self.assertEqual(before, {} if boundary == "reserved" else {
                    "sample.py": SOURCE.encode(), "notes.txt": b"retained fixture"})
                with sessions() as db:
                    if boundary == "after_commit":
                        record = db.query(Repository).one()
                        self.assertEqual(record.clone_url, URL)
                        self.assertEqual(record.last_indexed_commit, "fixture_commit")
                        file = db.query(CodeFile).one()
                        self.assertEqual(file.repository_id, record.id)
                        self.assertEqual(file.path, "sample.py")
                        self.assertEqual(file.content_hash, repository.calculate_file_hash(destination / "sample.py"))
                        self.assertEqual(file.last_indexed_commit, "fixture_commit")
                    else:
                        self.assertEqual(db.query(Repository).count(), 0)
                        self.assertEqual(db.query(CodeFile).count(), 0)
                    self.assertEqual(db.query(CodeSymbol).count(), 0)
                    with patch.object(repository, "REPOSITORY_ROOT", clones), \
                         patch.object(repository, "git_output", side_effect=AssertionError("Retry cloned")) as git, \
                         patch.object(repository.shutil, "rmtree", side_effect=AssertionError("Retry removed clone")) as remove:
                        with self.assertRaises(RepositoryConflict):
                            repository.ingest_repository(db, URL)
                    git.assert_not_called()
                    remove.assert_not_called()
                self.assertTrue(destination.is_dir())
                self.assertEqual(before, {path.relative_to(destination).as_posix(): path.read_bytes()
                                         for path in destination.rglob("*") if path.is_file()})
                with sessions() as verify:
                    expected = int(boundary == "after_commit")
                    self.assertEqual(verify.query(Repository).count(), expected)
                    self.assertEqual(verify.query(CodeFile).count(), expected)
            finally:
                sql.dispose()

    def test_exit_after_directory_reservation(self):
        self.exercise_boundary("reserved")

    def test_exit_after_clone(self):
        self.exercise_boundary("cloned")

    def test_exit_after_discovery(self):
        self.exercise_boundary("discovered")

    def test_exit_before_commit(self):
        self.exercise_boundary("before_commit")

    def test_exit_after_commit_before_response(self):
        self.exercise_boundary("after_commit")
