"""Ready-stage crash recovery with disk metadata and durable fake side effects."""
import json
from pathlib import Path
import tempfile
import unittest

from app.db.database import Base
from app.db.models import (IndexPublicationJob, Repository, RepositoryFullIndexJob,
                           SearchIndexGeneration)
from app.search.generation_lifecycle import utc_now
from recovery_support import _sessions, run_exit_child
from publication_process_fixture import _publish, SOURCE, STAGE


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
                run_exit_child(_publish, (str(database), str(external), boundary), exitcode=74)
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
