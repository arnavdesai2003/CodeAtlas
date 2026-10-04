"""Ready-stage crash recovery with disk metadata and durable fake side effects."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.db.database import Base
from app.db.models import (IndexPublicationJob, Repository, RepositoryFullIndexJob,
                           SearchIndexGeneration)
from app.search import engine, publication
from app.search.generation_lifecycle import utc_now
from test_sync_process_recovery import _sessions

SOURCE = engine.INDEX_NAME + "_generation_" + "a" * 32
STAGE = engine.INDEX_NAME + "_generation_" + "b" * 32


def _publish(database, external, boundary=None):
    sql, sessions = _sessions(database)
    state_path = Path(external)
    def read():
        return json.loads(state_path.read_text())
    def write(state):
        state_path.write_text(json.dumps(state))
    def stop(point):
        if boundary == point:
            os._exit(74)
    client = Mock()
    client.options.return_value = client
    client.indices.get_settings.side_effect = lambda **kw: {kw["index"]: {
        "settings": {"index": {"uuid": "uuid-" + kw["index"]}}}}
    client.count.return_value = {"count": 1, "_shards": {"failed": 0}}
    client.reindex.side_effect = AssertionError("Ready stage rebuilt")
    client.indices.create.side_effect = AssertionError("Ready stage recreated")
    def swap(**kwargs):
        stop("before_alias")
        state = read()
        state["active"] = kwargs["actions"][-1]["add"]["index"]
        state["switches"] += 1
        write(state)
        stop("after_alias")
        return {"acknowledged": True}
    client.indices.update_aliases.side_effect = swap
    def invalidate(*, strict):
        assert strict
        stop("before_cache")
        state = read()
        state["rotations"] += 1
        write(state)
        stop("after_cache")
        return 0
    try:
        with sessions() as db, \
             patch.object(engine, "elasticsearch_client", client), \
             patch.object(engine, "resolve_search_index", side_effect=lambda: read()["active"]), \
             patch.object(engine, "_write_repository_index", side_effect=AssertionError("Ready symbols rewritten")), \
             patch.object(publication, "alias_target", side_effect=lambda *a, **k: read()["active"]), \
             patch.object(publication, "invalidate_search_cache", side_effect=invalidate):
            commit = db.commit
            commits = 0
            def interrupted_commit():
                nonlocal commits
                commits += 1
                stop("before_published_commit" if commits == 1 else "before_final_commit")
                commit()
                if commits == 2:
                    stop("after_final_commit")
            with patch.object(db, "commit", side_effect=interrupted_commit):
                return engine.index_repository_in_elasticsearch(db, 1)
    finally:
        sql.dispose()


class PublicationProcessRecoveryTests(unittest.TestCase):
    def exercise_boundary(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metadata.sqlite"
            external = Path(directory) / "external.json"
            external.write_text(json.dumps({"active": SOURCE, "switches": 0, "rotations": 0}))
            sql, sessions = _sessions(database)
            try:
                Base.metadata.create_all(sql)
                with sessions() as db:
                    db.add(Repository(id=1, name="fixture", clone_url="https://github.com/test/fixture",
                                      last_indexed_commit="unchanged"))
                    db.flush()
                    db.add(RepositoryFullIndexJob(repository_id=1, stats={}))
                    db.add(IndexPublicationJob(id=1, repository_id=1, source_index=SOURCE,
                        staging_index=STAGE, phase="ready", stats={"symbols_indexed": 1,
                        "documents_copied": 0, "documents_total": 1}))
                    for name, state in ((SOURCE, "published"), (STAGE, "building")):
                        db.add(SearchIndexGeneration(index_name=name, index_uuid="uuid-" + name,
                            repository_id=1, state=state, created_at=utc_now()))
                    db.commit()
                child = multiprocessing.get_context("spawn").Process(
                    target=_publish, args=(str(database), str(external), boundary))
                try:
                    child.start()
                    child.join(15)
                    self.assertFalse(child.is_alive(), "Publication child exceeded budget.")
                    self.assertEqual(child.exitcode, 74)
                finally:
                    if child.is_alive():
                        child.terminate()
                        child.join(5)
                    if child.is_alive():
                        child.kill()
                        child.join(5)
                    child.close()
                state = json.loads(external.read_text())
                recorded_times = None
                with sessions() as db:
                    job = db.get(IndexPublicationJob, 1)
                    self.assertEqual(db.get(Repository, 1).last_indexed_commit, "unchanged")
                    if boundary == "after_final_commit":
                        self.assertIsNone(job)
                        self.assertIsNone(db.get(RepositoryFullIndexJob, 1))
                    else:
                        self.assertIsNotNone(job)
                        expected = "ready" if boundary in {
                            "before_alias", "after_alias", "before_published_commit"} else "published"
                        self.assertEqual(job.phase, expected)
                        self.assertEqual(job.staging_index, STAGE)
                        self.assertIsNotNone(db.get(RepositoryFullIndexJob, 1))
                        if expected == "published":
                            recorded_times = (db.get(SearchIndexGeneration, SOURCE).inactive_at,
                                              db.get(SearchIndexGeneration, STAGE).published_at)
                if boundary != "after_final_commit":
                    result = _publish(str(database), str(external))
                    self.assertTrue(result["resumed"])
                    self.assertEqual(result["index"], STAGE)
                final = json.loads(external.read_text())
                self.assertEqual(final["active"], STAGE)
                self.assertEqual(final["switches"], 1)
                expected_rotations = 2 if boundary in {"after_cache", "before_final_commit"} else 1
                self.assertEqual(final["rotations"], expected_rotations)
                self.assertEqual(state["active"], SOURCE if boundary == "before_alias" else STAGE)
                with sessions() as db:
                    self.assertIsNone(db.get(IndexPublicationJob, 1))
                    self.assertIsNone(db.get(RepositoryFullIndexJob, 1))
                    self.assertEqual(db.get(SearchIndexGeneration, SOURCE).state, "retired")
                    self.assertIsNotNone(db.get(SearchIndexGeneration, SOURCE).inactive_at)
                    self.assertEqual(db.get(SearchIndexGeneration, STAGE).state, "published")
                    self.assertIsNotNone(db.get(SearchIndexGeneration, STAGE).published_at)
                    if recorded_times is not None:
                        self.assertEqual(recorded_times,
                            (db.get(SearchIndexGeneration, SOURCE).inactive_at,
                             db.get(SearchIndexGeneration, STAGE).published_at))
                    self.assertEqual(db.get(Repository, 1).last_indexed_commit, "unchanged")
            finally:
                sql.dispose()

    def test_exit_before_alias_switch(self):
        self.exercise_boundary("before_alias")

    def test_exit_after_alias_switch_before_acknowledgement(self):
        self.exercise_boundary("after_alias")

    def test_exit_before_published_metadata_commit(self):
        self.exercise_boundary("before_published_commit")

    def test_exit_before_cache_rotation(self):
        self.exercise_boundary("before_cache")

    def test_exit_after_cache_rotation(self):
        self.exercise_boundary("after_cache")

    def test_exit_before_final_commit(self):
        self.exercise_boundary("before_final_commit")

    def test_exit_after_final_commit(self):
        self.exercise_boundary("after_final_commit")
