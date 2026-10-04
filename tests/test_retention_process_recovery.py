"""Reviewed cleanup process exits retain audit and reconcile exact identities."""
from datetime import timedelta
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.db.database import Base
from app.db.models import SearchIndexGeneration
from app.search import generation_retention as retention
from test_generation_retention import ROOT, ALIAS, NOW, name
from test_sync_process_recovery import _sessions


def _client(external, boundary=None):
    path = Path(external)
    client = Mock()
    client.info.return_value = {"cluster_uuid": "cluster"}
    client.indices.get_alias.return_value = {name(1): {"aliases": {ALIAS: {}}}}
    client.indices.get.side_effect = lambda **kw: json.loads(path.read_text())["indices"]
    client.indices.stats.return_value = {"_shards": {"failed": 0}, "indices": {}}
    client.tasks.list.return_value = {"nodes": {}}
    def delete(*, index):
        state = json.loads(path.read_text())
        if boundary == "before_delete" or (boundary == "before_second_delete" and state["deleted"]):
            os._exit(76)
        del state["indices"][index]
        state["deleted"].append(index)
        path.write_text(json.dumps(state))
        if boundary == "after_delete":
            os._exit(76)
        return {"acknowledged": True}
    client.indices.delete.side_effect = delete
    return client


def _apply(database, external, reviewed, boundary=None):
    sql, sessions = _sessions(database)
    try:
        with sessions() as db:
            commit = db.commit
            def interrupted_commit():
                if boundary == "before_audit_commit":
                    os._exit(76)
                commit()
                if boundary == "after_audit_commit":
                    os._exit(76)
            with patch.object(db, "commit", side_effect=interrupted_commit):
                return retention.apply_cleanup_plan(db, _client(external, boundary),
                    json.loads(Path(reviewed).read_text()), quiesced=True, now=NOW,
                    legacy_name=ROOT, alias_name=ALIAS)
    finally:
        sql.dispose()


class RetentionProcessRecoveryTests(unittest.TestCase):
    def exercise_boundary(self, boundary, *, recreate=False):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metadata.sqlite"
            external = Path(directory) / "indices.json"
            reviewed = Path(directory) / "plan.json"
            sql, sessions = _sessions(database)
            try:
                Base.metadata.create_all(sql)
                metadata = {}
                with sessions() as db:
                    for index, state, hours in ((ROOT, None, 0), (name(1), "published", 0),
                        (name(2), "retired", 26), (name(3), "retired", 48), (name(4), "abandoned", 48)):
                        metadata[index] = {"aliases": {ALIAS: {}} if index == name(1) else {},
                            "settings": {"index": {"uuid": "uuid-" + index}}}
                        if state:
                            db.add(SearchIndexGeneration(index_name=index, index_uuid="uuid-" + index,
                                state=state, created_at=NOW - timedelta(days=365),
                                inactive_at=NOW - timedelta(hours=hours) if hours else None))
                    db.commit()
                    external.write_text(json.dumps({"indices": metadata, "deleted": []}))
                    inventory = retention.inspect_generations(db, _client(external),
                        policy=retention.RetentionPolicy(24, 1), now=NOW, legacy_name=ROOT, alias_name=ALIAS)
                    plan = retention.make_cleanup_plan(inventory)
                    self.assertEqual([item["index_name"] for item in plan["candidates"]], [name(3), name(4)])
                    reviewed.write_text(json.dumps(plan))
                original_plan = reviewed.read_bytes()
                child = multiprocessing.get_context("spawn").Process(target=_apply,
                    args=(str(database), str(external), str(reviewed), boundary))
                try:
                    child.start()
                    child.join(15)
                    self.assertFalse(child.is_alive(), "Cleanup child exceeded budget.")
                    self.assertEqual(child.exitcode, 76)
                finally:
                    if child.is_alive():
                        child.terminate()
                        child.join(5)
                    if child.is_alive():
                        child.kill()
                        child.join(5)
                    child.close()
                before = json.loads(external.read_text())
                audit_committed = boundary in {"after_audit_commit", "before_second_delete"}
                with sessions() as db:
                    row = db.get(SearchIndexGeneration, name(3))
                    self.assertEqual(row.state, "deleted" if audit_committed else "retired")
                    self.assertEqual(row.deleted_at is not None, audit_committed)
                    deleted_at = row.deleted_at
                    self.assertEqual(db.get(SearchIndexGeneration, name(4)).state, "abandoned")
                if recreate:
                    before["indices"][name(3)] = {"aliases": {}, "settings": {"index": {"uuid": "recreated"}}}
                    external.write_text(json.dumps(before))
                    with self.assertRaises(retention.GenerationCleanupError):
                        _apply(str(database), str(external), str(reviewed))
                    self.assertEqual(json.loads(external.read_text()), before)
                    with sessions() as db:
                        self.assertEqual(db.get(SearchIndexGeneration, name(3)).state, "retired")
                        self.assertEqual(db.get(SearchIndexGeneration, name(4)).state, "abandoned")
                    return
                result = _apply(str(database), str(external), str(reviewed))
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["already_absent"], [] if boundary == "before_delete" else [name(3)])
                final = json.loads(external.read_text())
                self.assertEqual(final["deleted"], [name(3), name(4)])
                self.assertEqual(final["indices"], {index: metadata[index] for index in (ROOT, name(1), name(2))})
                self.assertEqual(reviewed.read_bytes(), original_plan)
                with sessions() as db:
                    self.assertEqual(db.query(SearchIndexGeneration).count(), 4)
                    for index in (name(3), name(4)):
                        row = db.get(SearchIndexGeneration, index)
                        self.assertEqual(row.state, "deleted")
                        self.assertIsNotNone(row.deleted_at)
                        self.assertEqual(row.index_uuid, "uuid-" + index)
                    if deleted_at:
                        self.assertEqual(db.get(SearchIndexGeneration, name(3)).deleted_at, deleted_at)
            finally:
                sql.dispose()

    def test_exit_before_delete(self):
        self.exercise_boundary("before_delete")

    def test_exit_after_delete_before_acknowledgement(self):
        self.exercise_boundary("after_delete")

    def test_exit_before_audit_commit(self):
        self.exercise_boundary("before_audit_commit")

    def test_exit_after_audit_commit(self):
        self.exercise_boundary("after_audit_commit")

    def test_exit_before_second_delete(self):
        self.exercise_boundary("before_second_delete")

    def test_recreated_identity_blocks_all_remaining_deletes(self):
        self.exercise_boundary("after_delete", recreate=True)
