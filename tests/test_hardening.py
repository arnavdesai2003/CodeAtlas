"""Offline regressions for accumulated response and JSON boundary changes."""
import json
import io
import os
from pathlib import Path
from contextlib import redirect_stdout
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.update(DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
                  ELASTICSEARCH_URL="http://127.0.0.1:9200", REDIS_URL="redis://127.0.0.1:6379/15",
                  HF_HUB_OFFLINE="1")

from elastic_transport import ApiResponseMeta, NodeConfig, ObjectApiResponse
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from app.api import routes
from app.search import cache, engine, service
from app.search.generation_retention import RetentionBlocked
from scripts import manage_index_generations
from test_cache_generation import MemoryRedis


def wrapped(body):
    return ObjectApiResponse(body=body, meta=ApiResponseMeta(
        200, "1.1", {}, 0, NodeConfig("http", "localhost", 9200)))


class CacheBoundaryTests(unittest.TestCase):
    def test_invalid_generation_replies_retrieve_without_fills_or_flights(self):
        for reply in (None, "ab", {}, [], ["g"], ["g", None, 1],
                      [None, None], [[], None], [False, "[]"], ["", "[]"]):
            with self.subTest(reply=reply), patch.object(cache, "redis_client") as redis, \
                 patch.object(service, "search_code", return_value=[]) as search, \
                 patch.object(service, "set_cached_search") as fill, \
                 patch.object(service, "_miss_flights") as flights:
                redis.eval.return_value = reply
                result = service.search_with_cache("unchanged query", 10)
                self.assertFalse(result["cache_hit"])
                self.assertFalse(result["cache_coalesced"])
                search.assert_called_once_with(query="unchanged query", limit=10)
                fill.assert_not_called()
                flights.acquire.assert_not_called()

    def test_invalid_write_tokens_never_contact_redis(self):
        with patch.object(cache, "redis_client") as redis:
            for token in (None, "", [], False, 1):
                with self.subTest(token=token):
                    self.assertFalse(cache.set_cached_search("q", 10, [], generation=token))
            redis.eval.assert_not_called()

    def test_duplicate_json_becomes_fenced_miss_and_healthy_refill(self):
        for value in ('[{"name":"old","name":"ambiguous"}]',
                      '[{"nested":{"name":"old","name":"ambiguous"}}]'):
            redis = MemoryRedis()
            redis.values[cache.GENERATION_KEY] = "original"
            key = cache.build_cache_key("q", 10, generation="original")
            redis.values[key] = value
            with self.subTest(value=value), patch.object(cache, "redis_client", redis), \
                 patch.object(service, "search_code", return_value=[{"name": "healthy"}]) as search:
                lookup = cache.get_cached_search("q", 10)
                self.assertIsNone(lookup.results)
                self.assertEqual(lookup.generation, "original")
                first = service.search_with_cache("q", 10)
                second = service.search_with_cache("q", 10)
                self.assertFalse(first["cache_hit"])
                self.assertTrue(second["cache_hit"])
                self.assertEqual(second["results"], [{"name": "healthy"}])
                search.assert_called_once()
                self.assertEqual(redis.values[cache.GENERATION_KEY], "original")

    def test_old_token_metacharacters_are_literal_in_cleanup_scan(self):
        for token, literal in (("old*", r"old\*"), ("old?", r"old\?"),
                               ("old[ab]", r"old\[ab\]"), ("old\\end", r"old\\end")):
            with self.subTest(token=token), patch.object(cache, "redis_client") as redis:
                redis.getset.return_value = token
                old_key = cache.build_cache_key("q", 10, generation=token)
                redis.scan_iter.return_value = iter([old_key])
                redis.delete.return_value = 1
                self.assertEqual(cache.invalidate_search_cache(strict=True), 1)
                redis.scan_iter.assert_called_once_with(match=f"{cache.ENTRY_PREFIX}{literal}:*", count=256)
                redis.delete.assert_called_once_with(old_key)
                self.assertEqual(redis.mock_calls[0][0], "getset")

    def test_bad_old_token_skips_cleanup_after_successful_rotation(self):
        for token in (None, "", [], False):
            with self.subTest(token=token), patch.object(cache, "redis_client") as redis:
                redis.getset.return_value = token
                self.assertEqual(cache.invalidate_search_cache(strict=True), 0)
                redis.getset.assert_called_once()
                redis.scan_iter.assert_not_called()


class SearchBoundaryTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        for target, attr, value in ((routes.settings, "app_env", "test"),
                                    (routes.settings, "api_key", SecretStr(""))):
            context = patch.object(target, attr, value)
            context.start()
            self.addCleanup(context.stop)

    def assert_rejected_without_fill(self, retrieve):
        with patch.object(service, "get_cached_search", return_value=cache.CacheLookup(generation="g")), \
             patch.object(service, "set_cached_search") as fill, \
             patch.object(service, "search_code", side_effect=retrieve):
            response = self.client.post("/search", json={"query": "q"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Search backend unavailable."})
        fill.assert_not_called()
        self.assertFalse(service._miss_flights._pending)

    def test_invalid_alias_routes_to_sanitized_503_without_cache_fill(self):
        with patch.object(engine, "elasticsearch_client") as es:
            es.indices.get_alias.return_value = wrapped({"private-index-name": {"aliases": {"wrong": {}}}})
            self.assert_rejected_without_fill(lambda **kwargs: engine.hybrid_search_weighted(**kwargs))

    def test_invalid_wrapped_completion_routes_to_503_and_releases_flights(self):
        for body in (None, {"timed_out": 0}, {"_shards": None},
                     {"_shards": {"failed": False}}, {"_shards": {"failed": -1}}):
            with self.subTest(body=body):
                self.assert_rejected_without_fill(lambda **kwargs: engine._complete_hits(wrapped(body)))
        self.assertEqual(engine._complete_hits(wrapped({"timed_out": False,
            "_shards": {"failed": 0}, "hits": {"hits": []}})), [])


class CleanupPlanJsonTests(unittest.TestCase):
    def test_cli_rejects_duplicate_plan_before_apply_or_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviewed.json"
            path.write_text('{"private-field":1,"private-field":2}', encoding="utf-8")
            output = io.StringIO()
            with patch.object(manage_index_generations, "SessionLocal") as session, \
                 patch.object(manage_index_generations, "elasticsearch_client") as client, \
                 patch.object(manage_index_generations, "apply_cleanup_plan") as apply, \
                 redirect_stdout(output):
                status = manage_index_generations.main(["apply", "--plan", str(path), "--quiesced"])
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(output.getvalue())["error"], "RetentionBlocked")
            self.assertNotIn("private-field", output.getvalue())
            apply.assert_not_called()
            client.options.return_value.info.assert_not_called()
            client.options.return_value.indices.delete.assert_not_called()
            session.return_value.close.assert_called_once()

    def test_duplicate_fields_at_any_level_are_rejected(self):
        for value in ('{"policy":{},"policy":{}}',
                      '{"policy":{"keep_retired":1,"keep_retired":2}}',
                      '{"candidates":[{"index_uuid":"one","index_uuid":"two"}]}'):
            with self.subTest(value=value), self.assertRaises(RetentionBlocked):
                json.loads(value, object_pairs_hook=manage_index_generations._unique_plan_object)
        self.assertEqual(json.loads('{"policy":{"keep_retired":2}}',
            object_pairs_hook=manage_index_generations._unique_plan_object), {"policy": {"keep_retired": 2}})
