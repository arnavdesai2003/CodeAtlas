"""Interrupted builds retain attempts and replay onto fresh generations."""
import json
from pathlib import Path
import tempfile
import unittest

from app.db.database import Base
from app.db.models import (IndexPublicationJob, Repository, RepositoryFullIndexJob,
                           SearchIndexGeneration)
from app.search.generation_lifecycle import utc_now
from publication_process_fixture import _publish, SOURCE, STAGE
from recovery_support import _sessions, run_exit_child


class PublicationBuildProcessRecoveryTests(unittest.TestCase):
    def exercise_boundary(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metadata.sqlite"
            external = Path(directory) / "external.json"
            external.write_text(json.dumps({"active": SOURCE, "switches": 0, "rotations": 0,
                "indices": {SOURCE: 1, STAGE: 0}, "copies": [], "writes": []}))
            sql, sessions = _sessions(database)
            try:
                Base.metadata.create_all(sql)
                with sessions() as db:
                    db.add(Repository(id=1, name="fixture", clone_url="https://github.com/test/fixture",
                                      last_indexed_commit="unchanged"))
                    db.flush()
                    db.add(RepositoryFullIndexJob(repository_id=1, stats={}))
                    db.add(IndexPublicationJob(id=1, repository_id=1, source_index=SOURCE,
                        staging_index=STAGE, phase="building", stats={}))
                    for name, state in ((SOURCE, "published"), (STAGE, "building")):
                        db.add(SearchIndexGeneration(index_name=name, index_uuid="uuid-" + name,
                            repository_id=1, state=state, created_at=utc_now()))
                    db.commit()
                run_exit_child(_publish, (str(database), str(external), boundary),
                    kwargs={"building": True}, exitcode=74)
                before = json.loads(external.read_text())
                self.assertEqual(before["active"], SOURCE)
                with sessions() as db:
                    job = db.get(IndexPublicationJob, 1)
                    interrupted = job.staging_index
                    self.assertNotEqual(interrupted, STAGE)
                    self.assertEqual(job.phase, "ready" if boundary == "after_ready_commit" else "building")
                    row = db.get(SearchIndexGeneration, interrupted)
                    self.assertEqual(row.state, "building")
                    self.assertEqual(row.index_uuid is None,
                                     boundary in {"after_build_journal_commit", "after_create"})
                    self.assertEqual(db.get(Repository, 1).last_indexed_commit, "unchanged")
                result = _publish(str(database), str(external), building=True)
                self.assertTrue(result["resumed"])
                final = json.loads(external.read_text())
                self.assertEqual(final["active"], result["index"])
                self.assertEqual(final["switches"], 1)
                self.assertEqual(final["rotations"], 1)
                self.assertEqual(final["indices"][SOURCE], 1)
                self.assertEqual(final["indices"][STAGE], 0)
                for name, count in before["indices"].items():
                    self.assertEqual(final["indices"][name], count, "Retry modified abandoned index")
                if boundary == "after_ready_commit":
                    self.assertEqual(result["index"], interrupted)
                    self.assertEqual(final["copies"], before["copies"])
                    self.assertEqual(final["writes"], before["writes"])
                else:
                    self.assertNotEqual(result["index"], interrupted)
                    self.assertEqual(final["copies"], before["copies"] + [result["index"]])
                    self.assertEqual(final["writes"], before["writes"] + [result["index"]])
                with sessions() as db:
                    self.assertIsNone(db.get(IndexPublicationJob, 1))
                    self.assertIsNone(db.get(RepositoryFullIndexJob, 1))
                    self.assertEqual(db.get(SearchIndexGeneration, STAGE).state, "abandoned")
                    if boundary != "after_ready_commit":
                        row = db.get(SearchIndexGeneration, interrupted)
                        self.assertEqual(row.state, "abandoned")
                        self.assertIsNotNone(row.inactive_at)
                    self.assertEqual(db.get(SearchIndexGeneration, result["index"]).state, "published")
            finally:
                sql.dispose()

    def test_exit_after_build_journal_commit(self):
        self.exercise_boundary("after_build_journal_commit")

    def test_exit_after_stage_creation_before_identity_commit(self):
        self.exercise_boundary("after_create")

    def test_exit_after_identity_commit(self):
        self.exercise_boundary("after_identity_commit")

    def test_exit_after_copy(self):
        self.exercise_boundary("after_copy")

    def test_exit_after_symbol_write(self):
        self.exercise_boundary("after_symbols")

    def test_exit_before_ready_commit(self):
        self.exercise_boundary("before_ready_commit")

    def test_exit_after_ready_commit(self):
        self.exercise_boundary("after_ready_commit")
