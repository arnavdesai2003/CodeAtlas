"""Deterministic cross-process races at ingestion's directory reservation."""
import multiprocessing
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from app.db.database import Base
from app.db.models import CodeFile, Repository
from app.indexer import repository
from app.indexer.errors import RepositoryCloneFailed, RepositoryConflict
from test_ingestion_process_recovery import SOURCE, URL
from test_sync_process_recovery import _sessions


def _ingest_contender(database, root, url, barrier, ready, release, pipe, fail_clone):
    sql, sessions = _sessions(database)
    destination = Path(root) / "owner" / "fixture"
    mkdir = Path.mkdir
    def reserve(path, *args, **kwargs):
        if path == destination:
            barrier.wait(15)
        return mkdir(path, *args, **kwargs)
    def clone(*args):
        (destination / "sample.py").write_text(SOURCE)
        ready.set()
        if not release.wait(15):
            raise RuntimeError("Owner release timed out.")
        if fail_clone:
            raise subprocess.CalledProcessError(1, ["git", "clone"], stderr="fixture failure")
    try:
        with sessions() as db, patch.object(Path, "mkdir", reserve), \
             patch.object(repository, "REPOSITORY_ROOT", Path(root)), \
             patch.object(repository, "git_output", side_effect=clone), \
             patch.object(repository, "get_current_commit", return_value="fixture_commit"), \
             patch.object(repository, "get_current_branch", return_value="main"):
            try:
                result = repository.ingest_repository(db, url)
                pipe.send({"status": "committed", "id": result["repository_id"], "url": url})
            except RepositoryConflict:
                pipe.send({"status": "conflict"})
            except RepositoryCloneFailed:
                pipe.send({"status": "clone_failed"})
    except Exception as exc:
        pipe.send({"status": "unexpected", "error": type(exc).__name__})
    finally:
        pipe.close()
        sql.dispose()


class IngestionProcessRaceTests(unittest.TestCase):
    def exercise_race(self, *, variant=False, fail_clone=False):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metadata.sqlite"
            root = Path(directory) / "clones"
            (root / "owner").mkdir(parents=True)
            destination = root / "owner" / "fixture"
            sql, sessions = _sessions(database)
            ctx = multiprocessing.get_context("spawn")
            barrier, ready, release = ctx.Barrier(2), ctx.Event(), ctx.Event()
            children, pipes, child_pipes = [], [], []
            try:
                Base.metadata.create_all(sql)
                for url in (URL, URL + ".git" if variant else URL):
                    parent, child = ctx.Pipe(duplex=False)
                    process = ctx.Process(target=_ingest_contender,
                        args=(str(database), str(root), url, barrier, ready, release, child, fail_clone))
                    children.append(process)
                    pipes.append(parent)
                    child_pipes.append(child)
                    process.start()
                    child.close()
                self.assertTrue(ready.wait(15), "Neither contender reserved the destination.")
                deadline = time.monotonic() + 15
                loser = None
                while loser is None and time.monotonic() < deadline:
                    for index, pipe in enumerate(pipes):
                        if pipe.poll(.05):
                            self.assertEqual(pipe.recv(), {"status": "conflict"})
                            loser = index
                            break
                self.assertIsNotNone(loser, "Losing contender did not report conflict.")
                self.assertEqual((destination / "sample.py").read_text(), SOURCE)
                with sessions() as db:
                    self.assertEqual(db.query(Repository).count(), 0)
                release.set()
                winner_pipe = pipes[1 - loser]
                self.assertTrue(winner_pipe.poll(15), "Owner did not finish.")
                result = winner_pipe.recv()
                self.assertEqual(result["status"], "clone_failed" if fail_clone else "committed")
                for child in children:
                    child.join(5)
                    self.assertFalse(child.is_alive())
                    self.assertEqual(child.exitcode, 0)
                with sessions() as db:
                    self.assertEqual(db.query(Repository).count(), 0 if fail_clone else 1)
                    self.assertEqual(db.query(CodeFile).count(), 0 if fail_clone else 1)
                    if not fail_clone:
                        record = db.query(Repository).one()
                        self.assertEqual(record.id, result["id"])
                        self.assertEqual(record.clone_url, result["url"])
                        self.assertEqual(db.query(CodeFile).one().repository_id, record.id)
                self.assertEqual(destination.exists(), not fail_clone)
                if not fail_clone:
                    self.assertEqual((destination / "sample.py").read_text(), SOURCE)
            finally:
                release.set()
                for child in children:
                    if child.pid is not None:
                        if child.is_alive():
                            child.terminate()
                        child.join(5)
                        if child.is_alive():
                            child.kill()
                            child.join(5)
                    child.close()
                for pipe in pipes + child_pipes:
                    pipe.close()
                sql.dispose()

    def test_same_url_reservation_has_one_owner(self):
        self.exercise_race()

    def test_url_variants_sharing_directory_have_one_owner(self):
        self.exercise_race(variant=True)

    def test_loser_preserves_owner_until_owner_clone_failure_cleanup(self):
        self.exercise_race(fail_clone=True)
