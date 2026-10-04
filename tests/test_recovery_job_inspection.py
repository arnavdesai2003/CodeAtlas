"""Metadata-only pending-job inspection and conservative resume hints."""
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from app.db.database import Base
from app.db.models import IndexPublicationJob, Repository, RepositoryFullIndexJob, RepositorySyncJob
from recovery_support import _sessions
from scripts import inspect_recovery_jobs as command


class RecoveryJobInspectionTests(unittest.TestCase):
    def setUp(self):
        self.sql, sessions = _sessions(":memory:")
        Base.metadata.create_all(self.sql)
        self.db = sessions()
        self.addCleanup(self.sql.dispose)
        self.addCleanup(self.db.close)
        self.db.add_all([Repository(id=value, name=str(value), clone_url=f"https://github.com/test/{value}")
                         for value in (1, 2, 3)])
        self.db.commit()

    def sync_job(self, value):
        return RepositorySyncJob(repository_id=value, old_commit="old", target_commit="target",
                                 affected_paths=["private.py"], file_ids=[1], stats={"private": "value"})

    def test_empty_report_performs_no_commit(self):
        with patch.object(self.db, "commit") as commit:
            report = command.inspect_recovery_jobs(self.db)
        self.assertEqual(report["sync_jobs"], [])
        self.assertFalse(report["publication_blocks_other_writers"])
        commit.assert_not_called()

    def test_malformed_path_metadata_cannot_produce_counts_or_hints(self):
        job = self.sync_job(1)
        self.db.add(job)
        self.db.commit()
        for value in ("private.py", {}, None, [None], [""], [1]):
            with self.subTest(value=value):
                job.affected_paths = value
                self.db.commit()
                with self.assertRaisesRegex(RuntimeError, "path metadata"):
                    command.inspect_recovery_jobs(self.db)

    def test_malformed_file_metadata_cannot_produce_counts_or_hints(self):
        job = self.sync_job(1)
        self.db.add(job)
        self.db.commit()
        for value in ("1", {}, None, [True], [0], [-1], [2147483648], ["1"]):
            with self.subTest(value=value):
                job.file_ids = value
                self.db.commit()
                with self.assertRaises((RuntimeError, ValueError)):
                    command.inspect_recovery_jobs(self.db)

    def test_help_exits_before_database_access(self):
        with patch.object(command, "SessionLocal") as session, redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                command.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("Elasticsearch or Redis", output.getvalue())
        session.assert_not_called()

    def test_unknown_arguments_exit_before_database_access(self):
        with patch.object(command, "SessionLocal") as session, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                command.main(["--unsupported"])
        self.assertEqual(result.exception.code, 2)
        session.assert_not_called()

    def test_pending_jobs_include_counts_without_paths_or_raw_stats(self):
        self.db.add(self.sync_job(2))
        self.db.add(RepositoryFullIndexJob(repository_id=1, stats={"private": "value"}))
        self.db.commit()
        report = command.inspect_recovery_jobs(self.db)
        self.assertEqual(report["sync_jobs"][0]["resume"], "POST /repositories/2/sync")
        self.assertIsNone(report["sync_jobs"][0]["blocked_reason"])
        self.assertEqual(report["sync_jobs"][0]["affected_path_count"], 1)
        self.assertIn("--repository-id 1", report["full_index_jobs"][0]["resume"])
        self.assertNotIn("private", str(report))

    def test_publication_hints_prioritize_owner_and_block_other_work(self):
        self.db.add(self.sync_job(2))
        self.db.add_all([RepositoryFullIndexJob(repository_id=value, stats={}) for value in (1, 3)])
        self.db.add(IndexPublicationJob(id=1, repository_id=1, phase="ready", source_index="source",
                                       staging_index="stage", stats={}))
        self.db.commit()
        report = command.inspect_recovery_jobs(self.db)
        self.assertTrue(report["publication_blocks_other_writers"])
        self.assertIsNone(report["sync_jobs"][0]["resume"])
        self.assertEqual(report["sync_jobs"][0]["blocked_reason"], "pending_publication_blocks_sync")
        self.assertIsNone(report["full_index_jobs"][1]["resume"])
        self.assertEqual(report["full_index_jobs"][1]["blocked_reason"], "another_repository_owns_publication")
        self.assertIn("--repository-id 1", report["publication_jobs"][0]["resume"])

    def test_conflicting_journals_do_not_offer_resume_commands(self):
        self.db.add(self.sync_job(1))
        self.db.add(RepositoryFullIndexJob(repository_id=1, stats={}))
        self.db.commit()
        report = command.inspect_recovery_jobs(self.db)
        self.assertEqual(report["conflicting_sync_and_full_repository_ids"], [1])
        self.assertIsNone(report["sync_jobs"][0]["resume"])
        self.assertIsNone(report["full_index_jobs"][0]["resume"])
        self.assertEqual(report["full_index_jobs"][0]["blocked_reason"], "conflicting_sync_and_full_jobs")

    def test_unknown_publication_phase_withholds_owner_resume_hints(self):
        self.db.add(RepositoryFullIndexJob(repository_id=1, stats={}))
        self.db.add(IndexPublicationJob(id=1, repository_id=1, phase="unknown", source_index="source",
                                       staging_index="stage", stats={}))
        self.db.commit()
        report = command.inspect_recovery_jobs(self.db)
        self.assertEqual(report["publication_jobs"][0]["phase"], "unknown")
        self.assertIsNone(report["publication_jobs"][0]["resume"])
        self.assertEqual(report["publication_jobs"][0]["blocked_reason"], "unknown_publication_phase")
        self.assertIsNone(report["full_index_jobs"][0]["resume"])
        self.assertEqual(report["full_index_jobs"][0]["blocked_reason"], "unknown_publication_phase")

    def test_publication_owner_sync_conflict_is_explicit(self):
        self.db.add(self.sync_job(1))
        self.db.add(IndexPublicationJob(id=1, repository_id=1, phase="ready", source_index="source",
                                       staging_index="stage", stats={}))
        self.db.commit()
        report = command.inspect_recovery_jobs(self.db)
        self.assertEqual(report["conflicting_publication_and_sync_repository_ids"], [1])
        self.assertEqual(report["publication_jobs"][0]["blocked_reason"], "publication_owner_has_pending_sync")
        self.assertIsNone(report["publication_jobs"][0]["resume"])
        self.assertIsNone(report["sync_jobs"][0]["resume"])

    def test_postgres_snapshot_is_read_only_before_journal_reads(self):
        db = Mock()
        db.get_bind.return_value.dialect.name = "postgresql"
        db.scalars.return_value.all.return_value = []
        command.inspect_recovery_jobs(db)
        self.assertIn("REPEATABLE READ, READ ONLY", str(db.execute.call_args.args[0]))
        db.commit.assert_not_called()

    def test_cli_failure_is_sanitized_nonzero_and_closes_session(self):
        db = Mock()
        db.__enter__ = Mock(return_value=db)
        db.__exit__ = Mock(return_value=False)
        output = io.StringIO()
        with patch.object(command, "SessionLocal", return_value=db), \
             patch.object(command, "inspect_recovery_jobs", side_effect=RuntimeError("secret")), redirect_stdout(output):
            self.assertEqual(command.main([]), 1)
        self.assertNotIn("secret", output.getvalue())
        db.__exit__.assert_called_once()
