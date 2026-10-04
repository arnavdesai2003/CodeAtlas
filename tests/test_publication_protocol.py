"""Offline safety/failure checks for scratch publication verification."""
import io
import json
import os
from contextlib import redirect_stdout
import unittest
from unittest.mock import Mock, patch

os.environ.update(DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
                  ELASTICSEARCH_URL="http://127.0.0.1:9200", REDIS_URL="redis://127.0.0.1:6379/15",
                  HF_HUB_OFFLINE="1")
from app.search import cache, engine
from scripts import verify_publication_protocol as protocol
from test_cache_generation import MemoryRedis
from sqlalchemy.orm import Session


class PublicationProtocolTests(unittest.TestCase):
    def test_cleanup_prevalidates_every_identity_before_any_deletion(self):
        client = Mock()
        client.indices.get.return_value = {"scratch": {}, "scratch_generation_one": {}}
        with patch.object(protocol, "index_uuid", side_effect=["source-uuid", "replacement-uuid"]), \
             patch.object(protocol, "_assert_idle") as idle:
            with self.assertRaisesRegex(RuntimeError, "identity was unverified"):
                protocol._clear_indices(client, "scratch", {
                    "scratch": "source-uuid", "scratch_generation_one": "reviewed-uuid"})
        client.indices.delete.assert_not_called()
        idle.assert_not_called()

    def test_cleanup_rechecks_identity_after_task_inspection(self):
        client = Mock()
        client.indices.get.return_value = {"scratch": {}}
        with patch.object(protocol, "index_uuid", side_effect=["reviewed", "replacement"]), \
             patch.object(protocol, "_assert_idle"):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                protocol._clear_indices(client, "scratch", {"scratch": "reviewed"})
        client.indices.delete.assert_not_called()

    def test_cleanup_refuses_unexpected_names_and_active_write_tasks(self):
        client = Mock()
        client.indices.get.return_value = {"scratch_external": {}}
        with self.assertRaisesRegex(RuntimeError, "Unexpected scratch index name"):
            protocol._clear_indices(client, "scratch", {"scratch_external": "uuid"})
        client.indices.get.return_value = {"scratch": {"aliases": {"external": {}}}}
        with self.assertRaisesRegex(RuntimeError, "unexpected alias"):
            protocol._clear_indices(client, "scratch", {"scratch": "uuid"})
        client.indices.get.return_value = {"scratch": {}}
        with patch.object(protocol, "index_uuid", return_value="uuid"), \
             patch.object(protocol, "_assert_idle", side_effect=RuntimeError("active tasks")):
            with self.assertRaisesRegex(RuntimeError, "active tasks"):
                protocol._clear_indices(client, "scratch", {"scratch": "uuid"})
        client.indices.delete.assert_not_called()

    def test_cleanup_requires_boolean_delete_ack_and_stops_after_ambiguity(self):
        client = Mock()
        client.indices.get.return_value = {"scratch": {}, "scratch_generation_one": {}}
        client.indices.delete.return_value = {"acknowledged": 1}
        with patch.object(protocol, "index_uuid", return_value="uuid"), \
             patch.object(protocol, "_assert_idle"):
            with self.assertRaisesRegex(RuntimeError, "deletion was ambiguous"):
                protocol._clear_indices(client, "scratch", {
                    "scratch": "uuid", "scratch_generation_one": "uuid"})
        client.indices.delete.assert_called_once_with(index="scratch")

    def test_successful_cleanup_uses_only_exact_validated_names(self):
        client = Mock()
        client.indices.get.side_effect = [{"scratch": {}, "scratch_generation_one": {}}, {}]
        client.indices.delete.return_value = {"acknowledged": True}
        with patch.object(protocol, "index_uuid", return_value="uuid"), \
             patch.object(protocol, "_assert_idle"):
            protocol._clear_indices(client, "scratch", {
                "scratch": "uuid", "scratch_generation_one": "uuid"})
        self.assertEqual([call.kwargs for call in client.indices.delete.call_args_list],
                         [{"index": "scratch"}, {"index": "scratch_generation_one"}])

    def test_lost_ack_injection_happens_after_real_delegated_switch(self):
        client = Mock()
        client.options.return_value = client
        client.indices.update_aliases.return_value = {"acknowledged": True}
        state = {"copies": 0, "switches": 0, "lose_ack": True}
        observed = protocol.ObservedClient(client, state).options(request_timeout=60)
        with self.assertRaises(TimeoutError):
            observed.indices.update_aliases(actions=[{"add": {"index": "scratch", "alias": "scratch_active"}}])
        client.indices.update_aliases.assert_called_once()
        self.assertEqual(state, {"copies": 0, "switches": 1})
        self.assertEqual(observed.indices.update_aliases(actions=[]), {"acknowledged": True})
        observed.reindex(source={"index": "scratch"}, dest={"index": "scratch_generation_one"})
        self.assertEqual(state, {"copies": 1, "switches": 2})

    def test_mid_verification_failure_cleans_private_artifacts_and_restores_globals(self):
        es = Mock()
        es.indices.exists.return_value = False
        es.count.return_value = {"count": 3, "_shards": {"failed": 0}}
        redis = MemoryRedis()
        redis.values[cache.GENERATION_KEY] = "normal"
        redis.values["normal:entry"] = "keep"
        before = dict(redis.values)
        globals_before = (engine.INDEX_NAME, engine.SEARCH_ALIAS, engine.elasticsearch_client,
                          cache.GENERATION_KEY, cache.ENTRY_PREFIX, cache.redis_client)
        snapshot = {"active": "normal", "uuid": "normal-uuid", "count": 3}
        with patch.object(protocol, "_normal_snapshot", return_value=snapshot), \
             patch.object(protocol, "index_uuid", return_value="scratch-uuid"), \
             patch.object(engine, "create_symbol_index"), \
             patch.object(engine, "_write_repository_index"), \
             patch.object(engine, "index_repository_in_elasticsearch", side_effect=RuntimeError("injected")), \
             patch.object(protocol, "_clear_indices") as cleanup, \
             patch.object(protocol, "alias_target", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                protocol.verify_publication_protocol(es, redis)
        self.assertEqual(redis.values, before)
        cleanup.assert_called_once()
        base = cleanup.call_args.args[1]
        self.assertEqual(cleanup.call_args.args[2], {base: "scratch-uuid"})
        self.assertEqual((engine.INDEX_NAME, engine.SEARCH_ALIAS, engine.elasticsearch_client,
                         cache.GENERATION_KEY, cache.ENTRY_PREFIX, cache.redis_client), globals_before)

    def test_cli_failure_is_nonzero_and_sanitized(self):
        output = io.StringIO()
        with patch.object(protocol, "verify_publication_protocol", side_effect=ConnectionError("private credentials")), \
             redirect_stdout(output):
            status = protocol.main()
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")
        self.assertNotIn("private credentials", output.getvalue())

    def test_metadata_rollback_failure_still_removes_private_redis_keys(self):
        es = Mock()
        es.indices.exists.return_value = False
        es.count.return_value = {"count": 3, "_shards": {"failed": 0}}
        redis = MemoryRedis()
        redis.values[cache.GENERATION_KEY] = "normal"
        before = dict(redis.values)
        with patch.object(protocol, "_normal_snapshot", return_value={}), \
             patch.object(protocol, "index_uuid", return_value="scratch-uuid"), \
             patch.object(engine, "create_symbol_index"), \
             patch.object(engine, "_write_repository_index"), \
             patch.object(engine, "index_repository_in_elasticsearch", side_effect=RuntimeError("injected")), \
             patch.object(Session, "rollback", side_effect=RuntimeError("metadata cleanup failed")), \
             patch.object(protocol, "_clear_indices") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "metadata cleanup failed"):
                protocol.verify_publication_protocol(es, redis)
        self.assertEqual(redis.values, before)
        cleanup.assert_not_called()
