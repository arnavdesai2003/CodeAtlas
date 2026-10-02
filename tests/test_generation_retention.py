"""Offline retention fault tests with real SQL transactions and simulated ES identities."""
import copy
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import Mock, patch

os.environ.update(DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
                  ELASTICSEARCH_URL="http://127.0.0.1:9200", REDIS_URL="redis://127.0.0.1:6379/15",
                  HF_HUB_OFFLINE="1")
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.db.database import Base
from app.db.models import Repository, SearchIndexGeneration, IndexPublicationJob, RepositoryFullIndexJob, RepositorySyncJob
from app.indexer.locking import RepositorySyncInProgress
from app.search.generation_retention import (GenerationCleanupError, RetentionBlocked, RetentionPolicy,
    inspect_generations, make_cleanup_plan, apply_cleanup_plan)

ROOT = "test_symbols"
ALIAS = "test_active"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def name(number):
    return f"{ROOT}_generation_{number:032x}"


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.sql = create_engine("sqlite://")
        self.addCleanup(self.sql.dispose)
        @event.listens_for(self.sql, "connect")
        def fk(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.sql)
        self.sessions = sessionmaker(self.sql, autoflush=False, expire_on_commit=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        repo = Repository(name="demo", clone_url="https://github.com/demo/demo")
        self.db.add(repo)
        self.db.commit()
        self.repo_id = repo.id
        self.meta = {}
        self.es = Mock()
        self.es.info.return_value = {"cluster_uuid": "cluster"}
        self.es.indices.get_alias.side_effect = lambda **kw: {name(1): {"aliases": {ALIAS: {}}}}
        self.es.indices.get.side_effect = lambda **kw: copy.deepcopy(self.meta)
        self.es.indices.stats.return_value = {"_shards": {"failed": 0}, "indices": {}}
        self.es.tasks.list.return_value = {"nodes": {}}
        def delete(**kw):
            del self.meta[kw["index"]]
            return {"acknowledged": True}
        self.delete = delete
        self.es.indices.delete.side_effect = delete
        self.add_index(ROOT, tracked=False)
        self.add_index(name(1), state="published", aliases={ALIAS: {}})
        self.add_index(name(2), hours=26)  # newest retired; retained
        self.add_index(name(3), hours=48)
        self.add_index(name(4), state="abandoned", hours=48)
        self.db.commit()
        self.policy = RetentionPolicy(24, 1)

    def add_index(self, index, *, tracked=True, state="retired", hours=48, aliases=None, identity=None):
        identity = identity or f"uuid-{index}"
        self.meta[index] = {"aliases": aliases or {}, "settings": {"index": {
            "uuid": identity, "creation_date": "1"}}}
        if tracked:
            self.db.add(SearchIndexGeneration(index_name=index, index_uuid=identity,
                state=state, repository_id=self.repo_id, created_at=NOW-timedelta(days=365),
                inactive_at=NOW-timedelta(hours=hours) if state in {"retired", "abandoned"} else None))

    def inspect(self):
        return inspect_generations(self.db, self.es, policy=self.policy, now=NOW,
                                   legacy_name=ROOT, alias_name=ALIAS)

    def plan(self):
        return make_cleanup_plan(self.inspect())

    def apply(self, plan=None, **kwargs):
        return apply_cleanup_plan(self.db, self.es, plan if plan is not None else self.plan(),
            now=NOW, legacy_name=ROOT, alias_name=ALIAS, **kwargs)

    def blocked(self, plan=None, **kwargs):
        with self.assertRaises(GenerationCleanupError) as caught:
            self.apply(plan, **kwargs)
        self.assertEqual(caught.exception.result["status"], "failed")
        return caught.exception.result

    def test_inspection_and_plan_are_read_only_and_report_protection(self):
        with patch.object(self.db, "commit") as commit:
            report = self.inspect()
            plan = make_cleanup_plan(report)
        commit.assert_not_called()
        self.es.indices.delete.assert_not_called()
        self.assertEqual([c["index_name"] for c in plan["candidates"]], [name(3), name(4)])
        rows = {r["index_name"]: r for r in report["indices"]}
        self.assertIn("active index", rows[name(1)]["protected_reasons"])
        self.assertIn("legacy index", rows[ROOT]["protected_reasons"])
        self.assertIn("retained published generation", rows[name(2)]["protected_reasons"])
        self.assertIsNone(rows[name(3)]["documents"])

    def test_age_uses_inactive_time_and_unknown_history_is_protected(self):
        cases = [(5, {"hours": 2}), (6, {"tracked": False}), (7, {"state": "building"}),
                 (8, {"state": "published"}), (9, {"hours": -1}), (10, {"aliases": {"other": {}}})]
        for number, args in cases:
            self.add_index(name(number), **args)
        self.add_index(f"{ROOT}_generation_not-managed")
        self.db.commit()
        self.assertEqual([c["index_name"] for c in self.plan()["candidates"]], [name(2), name(3), name(4)])

    def test_unknown_and_mismatched_identities_or_inactive_times_are_protected(self):
        for number in (3, 4, 5):
            if number == 5:
                self.add_index(name(number), state="abandoned")
                self.db.flush()
            row = self.db.get(SearchIndexGeneration, name(number))
            if number == 3:
                row.index_uuid = None
            elif number == 4:
                self.meta[name(number)]["settings"]["index"]["uuid"] = "recreated"
            else:
                row.inactive_at = None
        self.db.commit()
        self.assertEqual(self.plan()["candidates"], [])

    def test_statistics_failures_or_missing_active_block_cleanup(self):
        self.es.indices.stats.return_value = {"_shards": {"failed": 1}}
        with self.assertRaises(RetentionBlocked):
            self.inspect()
        self.es.indices.stats.return_value = {}
        del self.meta[name(1)]
        self.assertTrue(self.inspect()["issues"])
        self.blocked(quiesced=True)
        self.es.indices.delete.assert_not_called()

    def test_pending_publication_protects_references_and_blocks_apply(self):
        self.db.add(IndexPublicationJob(id=1, repository_id=self.repo_id, source_index=name(3),
                                       staging_index=name(4), phase="ready", stats={}))
        self.db.commit()
        report = self.inspect()
        for row in report["indices"]:
            if row["index_name"] in {name(3), name(4)}:
                self.assertIn("publication journal reference", row["protected_reasons"])
        self.assertIn(str(self.repo_id), report["publication_job"]["resume_command"])
        self.blocked(quiesced=True)
        self.es.indices.delete.assert_not_called()

    def test_pending_full_or_sync_jobs_block_previously_reviewed_plan(self):
        plan = self.plan()
        for job in (RepositoryFullIndexJob(repository_id=self.repo_id, stats={}),
                    RepositorySyncJob(repository_id=self.repo_id, old_commit="a", target_commit="b",
                        affected_paths=[], file_ids=[], stats={})):
            with self.subTest(job=type(job).__name__):
                self.db.add(job)
                self.db.commit()
                self.blocked(plan, quiesced=True)
                self.es.indices.delete.assert_not_called()
                self.db.delete(job)
                self.db.commit()

    def test_quiescence_and_global_writer_exclusion_required(self):
        result = self.blocked()
        self.assertIn("quiescence", result["detail"])
        with patch("app.search.generation_retention.generation_maintenance_lock",
                   side_effect=RepositorySyncInProgress("busy")):
            self.blocked(quiesced=True)
        self.es.indices.delete.assert_not_called()

    def test_plan_validation_rejects_wrong_cluster_namespace_duplicates_and_wildcards(self):
        plan = self.plan()
        variants = []
        for key, value in (("cluster_uuid", "elsewhere"), ("legacy_name", "elsewhere"),
                           ("version", True), ("policy", {"keep_retired": 0}), ("candidates", None)):
            item = copy.deepcopy(plan)
            item[key] = value
            variants.append(item)
        duplicate = copy.deepcopy(plan)
        duplicate["candidates"].append(copy.deepcopy(duplicate["candidates"][0]))
        variants.append(duplicate)
        for field, value in (("index_name", ROOT+"_generation_*"), ("index_uuid", None),
                             ("state", "published"), ("inactive_at", None)):
            item = copy.deepcopy(plan)
            item["candidates"][0][field] = value
            variants.append(item)
        for item in variants:
            with self.subTest(plan=item):
                self.blocked(item, quiesced=True)
                self.es.indices.delete.assert_not_called()

    def test_all_candidates_validated_before_first_delete(self):
        plan = self.plan()
        self.meta[name(4)]["aliases"] = {"held": {}}
        self.blocked(plan, quiesced=True)
        self.es.indices.delete.assert_not_called()

    def test_reviewed_lifecycle_change_rejects_plan(self):
        plan = self.plan()
        row = self.db.get(SearchIndexGeneration, name(4))
        row.inactive_at -= timedelta(hours=1)
        self.db.commit()
        self.blocked(plan, quiesced=True)
        self.es.indices.delete.assert_not_called()

    def test_write_tasks_and_incomplete_task_inspection_fail_closed(self):
        for response in ({"nodes": {"n": {"tasks": {"t": {}}}}}, {"node_failures": ["failed"], "nodes": {}},
                         {"task_failures": ["failed"], "nodes": {}}, {}):
            with self.subTest(response=response):
                self.es.tasks.list.return_value = response
                self.blocked(quiesced=True)
                self.es.indices.delete.assert_not_called()
        self.es.tasks.list.side_effect = ConnectionError("private connection information")
        result = self.blocked(quiesced=True)
        self.assertNotIn("private", result["detail"])

    def test_success_deletes_exact_indices_retains_audit_and_repeated_plan_is_idempotent(self):
        plan = self.plan()
        result = self.apply(plan, quiesced=True)
        self.assertEqual(result["deleted"], [name(3), name(4)])
        self.assertEqual(set(self.meta), {ROOT, name(1), name(2)})
        for index in result["deleted"]:
            row = self.db.get(SearchIndexGeneration, index)
            self.assertEqual(row.state, "deleted")
            self.assertEqual(row.index_uuid, f"uuid-{index}")
            self.assertIsNotNone(row.deleted_at)
            self.assertIsNotNone(row.inactive_at)
        again = self.apply(plan, quiesced=True)
        self.assertEqual(again["already_absent"], [name(3), name(4)])
        self.assertEqual(self.es.indices.delete.call_count, 2)

    def test_identity_is_rechecked_between_deletions_without_expanding_plan(self):
        plan = self.plan()
        def delete(**kw):
            response = self.delete(**kw)
            self.meta[name(4)]["settings"]["index"]["uuid"] = "recreated"
            return response
        self.es.indices.delete.side_effect = delete
        result = self.blocked(plan, quiesced=True)
        self.assertEqual(result["deleted"], [name(3)])
        self.assertEqual(result["failed_index"], name(4))
        self.assertIn(name(4), self.meta)
        self.assertEqual(self.es.indices.delete.call_count, 1)

    def test_lost_delete_ack_reconciles_on_retry_without_deleting_recreated_index(self):
        plan = self.plan()
        def delete(**kw):
            self.delete(**kw)
            raise TimeoutError("private URL")
        self.es.indices.delete.side_effect = delete
        result = self.blocked(plan, quiesced=True)
        self.assertEqual(result["failed_index"], name(3))
        self.assertEqual(result["deleted"], [])
        self.assertNotIn("private", result["detail"])
        self.es.indices.delete.side_effect = self.delete
        retried = self.apply(plan, quiesced=True)
        self.assertEqual(retried["already_absent"], [name(3)])
        self.assertEqual(retried["deleted"], [name(4)])
        self.meta[name(3)] = {"aliases": {}, "settings": {"index": {"uuid": "new"}}}
        self.blocked(plan, quiesced=True)
        self.assertIn(name(3), self.meta)

    def test_unacknowledged_delete_stops_before_next_target(self):
        self.es.indices.delete.side_effect = lambda **kw: {"acknowledged": False}
        self.blocked(quiesced=True)
        self.assertEqual(self.es.indices.delete.call_count, 1)
        self.assertEqual(self.db.get(SearchIndexGeneration, name(3)).state, "retired")

    def test_failed_audit_commit_reports_deleted_index_and_can_be_reconciled(self):
        plan = self.plan()
        with patch.object(self.db, "commit", side_effect=RuntimeError("audit failure")):
            result = self.blocked(plan, quiesced=True)
        self.assertEqual(result["deleted"], [name(3)])
        self.assertEqual(self.db.get(SearchIndexGeneration, name(3)).state, "retired")
        retry = self.apply(plan, quiesced=True)
        self.assertEqual(retry["already_absent"], [name(3)])
        self.assertEqual(self.db.get(SearchIndexGeneration, name(3)).state, "deleted")

    def lose_audit_ack(self, plan):
        commit = self.db.commit

        def committed_but_lost():
            commit()
            raise RuntimeError("private database URL")

        with patch.object(self.db, "commit", side_effect=committed_but_lost):
            result = self.blocked(plan, quiesced=True)
        self.assertEqual(result["deleted"], [name(3)])
        self.assertEqual(result["failed_index"], name(3))
        self.assertNotIn("private", result["detail"])
        self.assertNotIn(name(3), self.meta)
        self.assertIn(name(4), self.meta)
        self.es.indices.delete.assert_called_once_with(index=name(3))
        self.db.close()

    def test_lost_audit_ack_reconciles_in_new_session_preserving_timestamp(self):
        plan = self.plan()
        self.lose_audit_ack(plan)
        with self.sessions() as retry:
            row = retry.get(SearchIndexGeneration, name(3))
            self.assertEqual(row.state, "deleted")
            deleted_at = row.deleted_at
            self.assertIsNotNone(deleted_at)
            result = apply_cleanup_plan(retry, self.es, plan, quiesced=True,
                now=NOW, legacy_name=ROOT, alias_name=ALIAS)
            self.assertEqual(result["already_absent"], [name(3)])
            self.assertEqual(result["deleted"], [name(4)])
            self.assertEqual(row.deleted_at, deleted_at)
            self.assertEqual(row.index_uuid, f"uuid-{name(3)}")
        self.assertEqual(self.es.indices.delete.call_count, 2)

    def test_recreated_index_after_lost_audit_ack_blocks_all_retry_deletions(self):
        plan = self.plan()
        self.lose_audit_ack(plan)
        self.meta[name(3)] = {"aliases": {}, "settings": {"index": {"uuid": "replacement"}}}
        self.es.indices.delete.reset_mock()
        with self.sessions() as retry:
            with self.assertRaises(GenerationCleanupError):
                apply_cleanup_plan(retry, self.es, plan, quiesced=True,
                    now=NOW, legacy_name=ROOT, alias_name=ALIAS)
            self.assertEqual(retry.get(SearchIndexGeneration, name(3)).state, "deleted")
        self.es.indices.delete.assert_not_called()
        self.assertIn(name(3), self.meta)
        self.assertIn(name(4), self.meta)

    def test_missing_index_with_changed_history_cannot_be_reconciled(self):
        plan = self.plan()
        del self.meta[name(3)]
        row = self.db.get(SearchIndexGeneration, name(3))
        row.inactive_at = NOW
        self.db.commit()
        self.blocked(plan, quiesced=True)
        self.es.indices.delete.assert_not_called()

    def test_policy_requires_positive_grace_and_at_least_one_retired_generation(self):
        for age, keep in ((0, 1), (float("nan"), 1), (True, 1), (24, 0), (24, True), (24, 1.5)):
            with self.subTest(age=age, keep=keep):
                with self.assertRaises(RetentionBlocked):
                    RetentionPolicy(age, keep)


class RetentionCommandTests(unittest.TestCase):
    def setUp(self):
        from scripts import manage_index_generations
        self.command = manage_index_generations
        self.db = Mock()
        self.session = patch.object(self.command, "SessionLocal", return_value=self.db).start()
        self.client = patch.object(self.command, "elasticsearch_client").start()
        self.addCleanup(patch.stopall)

    def invoke(self, args):
        from contextlib import redirect_stdout
        import io
        import json
        output = io.StringIO()
        with redirect_stdout(output):
            status = self.command.main(args)
        return status, json.loads(output.getvalue())

    def test_plan_file_is_exclusive_and_matches_read_only_result(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import json
        report = {"cluster_uuid": "cluster", "legacy_name": ROOT, "alias_name": ALIAS,
                  "observed_at": NOW.isoformat(), "policy": {"min_age_hours": 24, "keep_retired": 2},
                  "issues": [], "indices": []}
        with TemporaryDirectory() as directory, patch.object(self.command, "inspect_generations", return_value=report):
            output = Path(directory) / "review.json"
            status, result = self.invoke(["plan", "--output", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.read_text()), result)
            original = output.read_text()
            status, result = self.invoke(["plan", "--output", str(output)])
            self.assertEqual(status, 1)
            self.assertEqual(result["error"], "FileExistsError")
            self.assertEqual(output.read_text(), original)
        self.db.commit.assert_not_called()
        self.client.indices.delete.assert_not_called()

    def test_apply_without_attestation_returns_structured_failure(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            plan = Path(directory) / "review.json"
            plan.write_text("{}")
            status, result = self.invoke(["apply", "--plan", str(plan)])
        self.assertEqual(status, 1)
        self.assertEqual(result["error"], "RetentionBlocked")
        self.assertIn("quiescence", result["detail"])
        self.db.rollback.assert_called_once()

    def test_invalid_policy_explains_failure_without_service_calls(self):
        status, result = self.invoke(["inspect", "--keep-retired", "0"])
        self.assertEqual(status, 1)
        self.assertIn("at least one", result["detail"])
        self.client.info.assert_not_called()
