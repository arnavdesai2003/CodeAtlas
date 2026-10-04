"""Offline failure/cleanup checks for the isolated Redis protocol command."""
import io
import json
import os
from contextlib import redirect_stdout
import unittest
from unittest.mock import Mock, patch

os.environ.update(DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
                  ELASTICSEARCH_URL="http://127.0.0.1:9200", REDIS_URL="redis://127.0.0.1:6379/15",
                  HF_HUB_OFFLINE="1")
from app.search import cache
from scripts import verify_cache_protocol as protocol
from test_cache_generation import MemoryRedis


class CacheProtocolTests(unittest.TestCase):
    def test_cleanup_batches_exact_private_keys_and_preserves_other_namespaces(self):
        redis = MemoryRedis()
        prefix = "private:random:"
        redis.values = {f"{prefix}{i}": "value" for i in range(260)}
        redis.values["ordinary:cache:key"] = "keep"
        with patch.object(redis, "delete", wraps=redis.delete) as delete:
            protocol._clear_namespace(redis, prefix)
        self.assertEqual(redis.values, {"ordinary:cache:key": "keep"})
        self.assertEqual([len(call.args) for call in delete.call_args_list], [128, 128, 4])

    def test_cleanup_refuses_out_of_namespace_scan_results(self):
        client = Mock()
        client.scan_iter.return_value = iter(["ordinary:cache:key"])
        with self.assertRaisesRegex(RuntimeError, "Unexpected cleanup key"):
            protocol._clear_namespace(client, "private:random:")
        client.delete.assert_not_called()

    def test_verification_failure_removes_private_keys_and_restores_module_namespace(self):
        redis = MemoryRedis()
        redis.values[cache.GENERATION_KEY] = "normal-generation"
        redis.values["ordinary:cache:key"] = "keep"
        redis.pttl = Mock(return_value=-1)
        before = dict(redis.values)
        original_namespace = (cache.redis_client, cache.ENTRY_PREFIX, cache.GENERATION_KEY)
        namespaces = []
        with self.assertRaisesRegex(RuntimeError, "TTL was invalid"):
            protocol.verify_cache_protocol(redis, on_namespace=namespaces.append)
        self.assertEqual(len(namespaces), 1)
        self.assertTrue(namespaces[0].startswith("codeatlas:verification:cache:"))
        self.assertEqual(redis.values, before)
        self.assertEqual((cache.redis_client, cache.ENTRY_PREFIX, cache.GENERATION_KEY), original_namespace)

    def test_cleanup_failure_prevents_success_report(self):
        redis = MemoryRedis()
        redis.pttl = Mock(return_value=-1)
        with patch.object(protocol, "_clear_namespace", side_effect=RuntimeError("cleanup failed")) as cleanup:
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                protocol.verify_cache_protocol(redis)
        cleanup.assert_called_once()

    def test_cli_failure_is_nonzero_and_does_not_echo_connection_details(self):
        output = io.StringIO()
        with patch.object(protocol, "verify_cache_protocol", side_effect=ConnectionError("private credentials")), \
             redirect_stdout(output):
            status = protocol.main([])
        self.assertEqual(status, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"], "ConnectionError")
        self.assertNotIn("private credentials", output.getvalue())
