"""Offline regression tests; run python -m unittest discover -s tests -v."""

import asyncio
import ast
import os
from pathlib import Path
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

# Set test configuration before importing modules; never connect to these URLs.
os.environ.update(
    DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
    ELASTICSEARCH_URL="http://127.0.0.1:9200",
    REDIS_URL="redis://127.0.0.1:6379/15",
    HF_HUB_OFFLINE="1",
)

import httpx
from pydantic import SecretStr
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.indexer.parser import parse_python_source
from app.indexer.repository import parse_github_url
from app.indexer.incremental import parse_git_diff
from app.search import cache, engine, service
from scripts import benchmark_api
from app.search.errors import IncompleteSearchError, InvalidSearchResponseError, InvalidQueryEmbeddingError, InvalidRerankerOutputError
from elastic_transport import ConnectionError as ElasticsearchConnectionError, ApiResponseMeta, NodeConfig
from elasticsearch import ApiError
from app.indexer.errors import (
    InvalidRepositoryURL, RepositoryConflict, RepositoryCloneFailed,
    RepositoryNotFound, RepositoryCloneMissing, UnsafeClonePath,
)


class ServiceTests(unittest.TestCase):
    def test_bypass_never_reads_or_writes_redis(self):
        with patch.object(service, "get_cached_search") as read, \
             patch.object(service, "set_cached_search") as write, \
             patch.object(service, "search_code", return_value=[{"name": "x"}]) as search:
            result = service.search_with_cache("original query", 10, bypass_cache=True)
        read.assert_not_called()
        write.assert_not_called()
        search.assert_called_once_with(query="original query", limit=10)
        self.assertFalse(result["cache_hit"])
        self.assertIn("elasticsearch_latency_ms", result)

    def test_empty_cached_results_are_a_hit(self):
        with patch.object(service, "get_cached_search", return_value=cache.CacheLookup([], "g")), \
             patch.object(service, "search_code") as search:
            result = service.search_with_cache("q", 10)
        self.assertTrue(result["cache_hit"])
        search.assert_not_called()

    def test_miss_searches_and_caches(self):
        with patch.object(service, "get_cached_search", return_value=cache.CacheLookup(generation="g")), \
             patch.object(service, "search_code", return_value=[]) as search, \
             patch.object(service, "set_cached_search") as write:
            result = service.search_with_cache("q", 10)
        self.assertFalse(result["cache_hit"])
        search.assert_called_once_with(query="q", limit=10)
        write.assert_called_once_with(query="q", limit=10, results=[], generation="g")

    def test_engine_failure_is_not_cached(self):
        with patch.object(service, "get_cached_search", return_value=cache.CacheLookup(generation="g")), \
             patch.object(service, "search_code", side_effect=RuntimeError("offline")), \
             patch.object(service, "set_cached_search") as write:
            with self.assertRaises(RuntimeError):
                service.search_with_cache("q", 10)
        write.assert_not_called()


class CacheTests(unittest.TestCase):
    def test_deep_and_cyclic_writes_are_rejected_without_redis(self):
        nested = {}
        for _ in range(cache.MAX_CACHE_NESTING + 1):
            nested = {"nested": nested}
        cyclic = {}
        cyclic["self"] = cyclic
        with patch.object(cache, "redis_client") as redis:
            for value in (nested, cyclic):
                self.assertFalse(cache.set_cached_search("q", 10, [value], generation="g"))
            redis.eval.assert_not_called()

    def test_decoder_recursion_error_falls_back_without_losing_generation(self):
        with patch.object(cache, "redis_client") as redis, \
             patch.object(cache.json, "loads", side_effect=RecursionError("nested input")):
            redis.eval.return_value = ["g", "[]"]
            lookup = cache.get_cached_search("q", 10)
        self.assertIsNone(lookup.results)
        self.assertEqual(lookup.generation, "g")

    def test_excessively_nested_json_becomes_generation_bound_miss(self):
        import sys
        depth = sys.getrecursionlimit() + 100
        value = '[{"nested":' + '[' * depth + '0' + ']' * depth + '}]'
        with patch.object(cache, "redis_client") as redis:
            redis.eval.return_value = ["g", value]
            lookup = cache.get_cached_search("q", 10)
        self.assertIsNone(lookup.results)
        self.assertEqual(lookup.generation, "g")

    def test_nested_corruption_falls_back_and_allows_healthy_cache_fill(self):
        import sys
        depth = sys.getrecursionlimit() + 100
        value = '[{"nested":' + '[' * depth + '0' + ']' * depth + '}]'
        with patch.object(cache, "redis_client") as redis, \
             patch.object(service, "search_code", return_value=[]) as search, \
             patch.object(service, "set_cached_search") as write:
            redis.eval.return_value = ["g", value]
            response = service.search_with_cache("q", 10)
            self.assertFalse(response["cache_hit"])
            self.assertEqual(response["results"], [])
            search.assert_called_once_with(query="q", limit=10)
            write.assert_called_once_with(query="q", limit=10, results=[], generation="g")
            self.assertFalse(service._miss_flights._pending)
            redis.eval.return_value = ["g", "[]"]
            self.assertTrue(service.search_with_cache("q", 10)["cache_hit"])
            search.assert_called_once()

    def test_nonfinite_and_over_limit_entries_become_generation_bound_misses(self):
        for value in ('[{"score":NaN}]', '[{"score":Infinity}]', '[{"score":-Infinity}]',
                      '[{"nested":{"value":1e400}}]', '[{},{}]'):
            with self.subTest(value=value), patch.object(cache, "redis_client") as redis:
                redis.eval.return_value = ["generation", value]
                lookup = cache.get_cached_search("q", 1)
                self.assertIsNone(lookup.results)
                self.assertEqual(lookup.generation, "generation")

    def test_invalid_cache_writes_do_not_contact_redis(self):
        for results in ([{"score": float("nan")}], [{"nested": float("inf")}], [{}, {}], "invalid"):
            with self.subTest(results=results), patch.object(cache, "redis_client") as redis:
                self.assertFalse(cache.set_cached_search("q", 1, results, generation="g"))
                redis.eval.assert_not_called()

    def test_finite_cache_values_and_empty_results_still_work(self):
        with patch.object(cache, "redis_client") as redis:
            redis.eval.return_value = ["g", '[{"score":0.75,"code":"NaN is text"}]']
            self.assertEqual(cache.get_cached_search("q", 1).results[0]["score"], .75)
            redis.eval.return_value = ["g", "[]"]
            self.assertEqual(cache.get_cached_search("q", 1).results, [])

    def test_normalization_and_limit(self):
        self.assertEqual(cache.build_cache_key(" Q  Test ", 10, generation="g"), cache.build_cache_key("q test", 10, generation="g"))
        self.assertNotEqual(cache.build_cache_key("q", 10, generation="g"), cache.build_cache_key("q", 5, generation="g"))

    def test_outage_and_corrupt_json_fall_back(self):
        with patch.object(cache, "redis_client") as redis:
            redis.eval.side_effect = ConnectionError()
            self.assertIsNone(cache.get_cached_search("q", 10).generation)
            redis.eval.side_effect = None
            redis.eval.return_value = ["g", "not JSON"]
            self.assertIsNone(cache.get_cached_search("q", 10).results)
            redis.eval.side_effect = ConnectionError()
            self.assertFalse(cache.set_cached_search("q", 10, [], generation="g"))

    def test_invalidation_is_namespaced(self):
        with patch.object(cache, "redis_client") as redis:
            redis.getset.return_value = "old"
            redis.scan_iter.return_value = ["codeatlas:search:v2:old:a"]
            redis.delete.return_value = 1
            self.assertEqual(cache.invalidate_search_cache(), 1)
            redis.scan_iter.assert_called_once_with(match="codeatlas:search:v2:old:*", count=256)
            redis.delete.assert_called_once_with("codeatlas:search:v2:old:a")

    def test_strict_invalidation_surfaces_failure(self):
        with patch.object(cache, "redis_client") as redis:
            redis.getset.side_effect = ConnectionError("offline")
            self.assertEqual(cache.invalidate_search_cache(), 0)
            with self.assertRaises(ConnectionError):
                cache.invalidate_search_cache(strict=True)


class ApiTests(unittest.TestCase):
    def test_search_query_boundary_preserves_exact_text(self):
        query = "  " + "é" * 4092 + "\n "
        result = self.client.post("/search", json={"query": query})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["query"], query)
        self.search.assert_called_once_with(query=query, limit=10, bypass_cache=False)

    def test_excessive_or_blank_queries_never_reach_service(self):
        for query in ("x" * 4097, "é" * 4097, " ", "\t\r\n", "\u2003\u00a0"):
            with self.subTest(length=len(query)):
                response = self.client.post("/search", json={"query": query})
                self.assertEqual(response.status_code, 422)
        self.search.assert_not_called()

    def test_query_length_is_documented_in_openapi(self):
        schema = self.client.app.openapi()["components"]["schemas"]["SearchRequest"]
        self.assertEqual(schema["properties"]["query"]["maxLength"], 4096)

    def test_invalid_clone_url_rejected_before_ingestion_services(self):
        for url in ("https://example.com/o/r", "https://[broken/o/r", "file:///tmp/repo",
                    "https://token@github.com/o/r", "https://github.com:443/o/r",
                    "https://github.com/o/r?token=secret", "https://github.com/o/%2e%2e"):
            with self.subTest(url=url):
                response = self.client.post("/repositories", json={"clone_url": url})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": "Invalid GitHub repository URL."})

    def test_creation_errors_are_typed_and_sanitized(self):
        for error, status in ((InvalidRepositoryURL("secret URL"), 400),
                              (UnsafeClonePath("private directory"), 409),
                              (RepositoryConflict("private clone path"), 409),
                              (RepositoryCloneFailed("Git stderr credentials"), 502),
                              (ValueError("internal parser secret"), 500),
                              (RuntimeError("ambiguous commit details"), 500)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(routes, "ingest_repository", side_effect=error):
                response = self.client.post("/repositories", json={"clone_url": "https://github.com/o/r"})
                self.assertEqual(response.status_code, status)
                self.assertNotIn(str(error), response.text)

    def test_sync_errors_do_not_misclassify_internal_failures(self):
        for error, status in ((RepositoryNotFound("private details"), 404),
                              (UnsafeClonePath("private directory"), 409),
                              (RepositoryCloneMissing("private path"), 409),
                              (routes.RepositorySyncInProgress("private journal"), 409),
                              (ValueError("parser failure"), 500),
                              (FileNotFoundError("model file"), 500),
                              (RuntimeError("backend token"), 500)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(routes, "sync_repository", side_effect=error):
                response = self.client.post("/repositories/1/sync")
                self.assertEqual(response.status_code, status)
                self.assertNotIn(str(error), response.text)

    def test_backend_failures_return_sanitized_503_and_recover(self):
        meta = ApiResponseMeta(503, "1.1", {}, 0.0, NodeConfig("http", "localhost", 9200))
        for error in (ElasticsearchConnectionError("private backend URL"),
                      ApiError("private failure", meta, {"secret": "backend details"}),
                      IncompleteSearchError("private index name"),
                      InvalidSearchResponseError("private score details"),
                      InvalidQueryEmbeddingError("private model output"),
                      InvalidRerankerOutputError("private reranker output")):
            with self.subTest(error=type(error).__name__):
                self.search.side_effect = error
                result = self.client.post("/search", json={"query": "q"})
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json(), {"detail": "Search backend unavailable."})
        self.search.side_effect = None
        self.assertEqual(self.client.post("/search", json={"query": "q"}).status_code, 200)

    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app, client=("127.0.0.1", 12345))
        self.addCleanup(self.client.close)
        self.search = patch.object(routes, "search_with_cache", return_value={
            "results": [], "cache_hit": False, "search_latency_ms": 2,
            "elasticsearch_latency_ms": 1,
        }).start()
        self.addCleanup(patch.stopall)

    def test_default_request_unchanged(self):
        response = self.client.post("/search", json={"query": "q"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-CodeAtlas-Cache-Bypassed", response.headers)
        self.search.assert_called_once_with(query="q", limit=10, bypass_cache=False)

    def test_coalesced_response_distinguishes_wait_from_cache_and_engine(self):
        self.search.return_value = {
            "results": [], "cache_hit": False, "cache_coalesced": True,
            "search_latency_ms": 4.5, "coalescing_wait_ms": 3.2,
        }
        result = self.client.post("/search", json={"query": "q"}).json()
        self.assertFalse(result["cache_hit"])
        self.assertTrue(result["cache_coalesced"])
        self.assertEqual(result["coalescing_wait_ms"], 3.2)
        self.assertIsNone(result["elasticsearch_latency_ms"])

    def test_bypass_requires_opt_in_development_and_loopback(self):
        for enabled, environment, host in [
            (False, "development", "127.0.0.1"),
            (True, "production", "127.0.0.1"),
            (True, "development", "192.0.2.1"),
        ]:
            with self.subTest(enabled=enabled, environment=environment, host=host), \
                 patch.object(routes.settings, "benchmark_cache_bypass_enabled", enabled), \
                 patch.object(routes.settings, "app_env", environment), \
                 patch.object(routes.settings, "api_key", SecretStr("test-api-key")), \
                 TestClient(self.client.app, client=(host, 12345)) as client:
                response = client.post("/search", json={"query": "q"}, headers={
                    "X-CodeAtlas-Benchmark-Bypass": "true", "X-Forwarded-For": "127.0.0.1",
                    "X-CodeAtlas-API-Key": "test-api-key",
                })
                self.assertEqual(response.status_code, 403)
        self.search.assert_not_called()

    def test_enabled_bypass_acknowledged(self):
        with patch.object(routes.settings, "benchmark_cache_bypass_enabled", True), \
             patch.object(routes.settings, "app_env", "development"):
            response = self.client.post("/search", json={"query": "q", "limit": 10},
                                        headers={"X-CodeAtlas-Benchmark-Bypass": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-CodeAtlas-Cache-Bypassed"], "true")
        self.search.assert_called_once_with(query="q", limit=10, bypass_cache=True)

    def test_validation(self):
        for payload in [{}, {"query": ""}, {"query": "q", "limit": 0}, {"query": "q", "limit": 101}]:
            self.assertEqual(self.client.post("/search", json=payload).status_code, 422)
        self.search.assert_not_called()

    def test_concurrent_sync_returns_conflict(self):
        with patch.object(routes, "sync_repository", side_effect=routes.RepositorySyncInProgress("busy")):
            response = self.client.post("/repositories/1/sync")
        self.assertEqual(response.status_code, 409)


class RetrievalTests(unittest.TestCase):
    def test_malformed_hit_collections_fail_with_typed_error(self):
        for response in ({}, {"hits": None}, {"hits": {}}, {"hits": {"hits": {}}}):
            with self.subTest(response=response), self.assertRaises(InvalidSearchResponseError):
                engine._complete_hits(response)
        for hits in (None, {}, [None], [{}]):
            with self.subTest(hits=hits), self.assertRaises(InvalidSearchResponseError):
                engine._format_hits(hits)

    def test_malformed_source_fields_fail_before_result_construction(self):
        valid = dict(repository="r", path="f.py", name="f", qualified_name="f",
                     kind="function", start_line=1, end_line=2, code="def f(): pass")
        changes = [{field: None} for field in ("repository", "path", "name", "qualified_name", "kind", "code")]
        changes += [{"start_line": True}, {"start_line": 0}, {"end_line": "2"},
                    {"end_line": 0}, {"language": []}, {"is_test": 1}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(InvalidSearchResponseError):
                engine._format_hits([{"_source": {**valid, **change}}])
        result = engine._format_hits([{"_source": valid}])[0]
        self.assertEqual(result["score"], 0)
        self.assertIsNone(result["language"])
        self.assertIs(result["is_test"], False)

    def test_malformed_source_never_fills_cache_and_releases_flight(self):
        with patch.object(engine, "elasticsearch_client") as es, \
             patch.object(service, "get_cached_search", return_value=cache.CacheLookup(generation="g")), \
             patch.object(service, "set_cached_search") as write, \
             patch.object(service, "search_code", side_effect=lambda **kw: engine.bm25_search(**kw, index_name="fixed")):
            es.options.return_value.search.return_value = {"hits": {"hits": [{"_source": {}}]}}
            with self.assertRaises(InvalidSearchResponseError):
                service.search_with_cache("q", 10)
            write.assert_not_called()
            self.assertFalse(service._miss_flights._pending)
            es.options.return_value.search.return_value = {"hits": {"hits": []}}
            self.assertEqual(service.search_with_cache("q", 10)["results"], [])
            write.assert_called_once()

    def test_invalid_query_vectors_never_search_or_fill_cache_and_can_retry(self):
        for vector in (None, [0.1] * 383, [float("nan")] * 384,
                       [float("inf")] * 384, [True] * 384, ["0.1"] * 384,
                       [10 ** 1000] * 384):
            with self.subTest(vector_type=type(vector).__name__), \
                 patch.object(engine, "elasticsearch_client") as es, \
                 patch.object(engine, "embed_text", return_value=vector) as embed, \
                 patch.object(service, "get_cached_search", return_value=cache.CacheLookup(generation="g")), \
                 patch.object(service, "set_cached_search") as write, \
                 patch.object(service, "search_code", side_effect=lambda **kw: engine.semantic_search(**kw, index_name="fixed")):
                es.options.return_value.search.return_value = {"hits": {"hits": []}}
                with self.assertRaises(InvalidQueryEmbeddingError):
                    service.search_with_cache("q", 10)
                es.options.return_value.search.assert_not_called()
                write.assert_not_called()
                self.assertFalse(service._miss_flights._pending)
                valid = [0.1] * 384
                embed.return_value = valid
                self.assertEqual(service.search_with_cache("q", 10)["results"], [])
                self.assertIs(es.options.return_value.search.call_args.kwargs["knn"]["query_vector"], valid)
                write.assert_called_once()

    def test_invalid_backend_scores_are_rejected(self):
        for score in (float("nan"), float("inf"), -float("inf"), True, "NaN", "1.0", 10 ** 400):
            with self.subTest(score=score), self.assertRaises(InvalidSearchResponseError):
                engine._format_hits([{"_source": {}, "_score": score}])

    def test_valid_scores_and_normalization_remain_unchanged(self):
        source = dict(repository="r", path="file.py", name="f", qualified_name="f",
                      kind="function", start_line=1, end_line=1, code="def f(): pass")
        for score, expected in ((None, 0.0), (0, 0.0), (-1.5, -1.5), (2, 2.0)):
            self.assertEqual(engine._format_hits([{"_source": source, "_score": score}])[0]["score"], expected)
        self.assertEqual(engine._min_max_normalize([2, 4, 6]), [0, .5, 1])
        self.assertEqual(engine._min_max_normalize([3, 3]), [1, 1])

    def test_invalid_score_normalization_never_fills_cache(self):
        for scores in ([float("nan")], [float("inf"), 1], [-1e308, 1e308]):
            with self.subTest(scores=scores), \
                 patch.object(service, "get_cached_search", return_value=cache.CacheLookup(generation="g")), \
                 patch.object(service, "set_cached_search") as write, \
                 patch.object(service, "search_code", side_effect=lambda **kw: engine._min_max_normalize(scores)):
                with self.assertRaises(InvalidSearchResponseError):
                    service.search_with_cache("q", 10)
                write.assert_not_called()
                self.assertFalse(service._miss_flights._pending)

    def test_hybrid_never_returns_surviving_branch_after_failure(self):
        for failing in ("bm25_search", "semantic_search"):
            with self.subTest(failing=failing), \
                 patch.object(engine, "resolve_search_index", return_value="fixed"), \
                 patch.object(engine, "bm25_search", return_value=[] ) as lexical, \
                 patch.object(engine, "semantic_search", return_value=[]) as semantic:
                (lexical if failing == "bm25_search" else semantic).side_effect = IncompleteSearchError("partial")
                with self.assertRaises(IncompleteSearchError):
                    engine.hybrid_search("q", 10)

    def test_incomplete_branches_fail_without_cache_fill_then_retry(self):
        for branch in (engine.bm25_search, engine.semantic_search):
            for failure in ({"timed_out": True}, {"_shards": {"failed": 1}}):
                with self.subTest(branch=branch.__name__, failure=failure), \
                     patch.object(engine, "elasticsearch_client") as es, \
                     patch.object(engine, "embed_text", return_value=[0.0] * 384), \
                     patch.object(service, "get_cached_search", return_value=cache.CacheLookup(generation="g")), \
                     patch.object(service, "set_cached_search") as write, \
                     patch.object(service, "search_code", side_effect=lambda **kw: branch(**kw, index_name="fixed")):
                    es.options.return_value.search.return_value = {"hits": {"hits": []}, **failure}
                    with self.assertRaises(IncompleteSearchError):
                        service.search_with_cache("q", 10)
                    write.assert_not_called()
                    self.assertFalse(service._miss_flights._pending)
                    self.assertIs(es.options.return_value.search.call_args.kwargs["allow_partial_search_results"], False)
                    es.options.return_value.search.return_value = {"timed_out": False,
                        "_shards": {"failed": 0}, "hits": {"hits": []}}
                    self.assertEqual(service.search_with_cache("q", 10)["results"], [])
                    write.assert_called_once()

    def setUp(self):
        resolver = patch.object(engine, "resolve_search_index", return_value="concrete")
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_incomplete_path_deletion_is_not_success(self):
        for response in [{"timed_out": True}, {"failures": [{"reason": "failed"}]}, {"version_conflicts": 1}]:
            with self.subTest(response=response), \
                 patch.object(engine, "create_symbol_index"), \
                 patch.object(engine, "elasticsearch_client") as es:
                es.options.return_value.delete_by_query.return_value = response
                with self.assertRaises(RuntimeError):
                    engine.delete_paths_from_elasticsearch(1, ["a.py"])

    def test_successful_path_deletion(self):
        with patch.object(engine, "create_symbol_index"), patch.object(engine, "elasticsearch_client") as es:
            es.options.return_value.delete_by_query.return_value = {
                "timed_out": False, "failures": [], "version_conflicts": 0,
            }
            engine.delete_paths_from_elasticsearch(1, ["a.py"])
            es.options.return_value.delete_by_query.assert_called_once()

    def test_fusion_deduplicates_and_weights(self):
        def hit(name, score):
            return dict(repository="repo", path=name, start_line=1, score=score)
        with patch.object(engine, "bm25_search", return_value=[hit("a", 10), hit("b", 2)]) as bm25, \
             patch.object(engine, "semantic_search", return_value=[hit("b", .9), hit("c", .5)]) as semantic:
            results = engine.hybrid_search_weighted("q", 10, .6)
        self.assertEqual([r["path"] for r in results], ["b", "a", "c"])
        self.assertEqual([r["score"] for r in results], [.6, .4, 0])
        bm25.assert_called_once_with("q", 40, index_name="concrete")
        semantic.assert_called_once_with("q", 40, index_name="concrete")

    def test_bm25_filters_tests_unless_requested(self):
        with patch.object(engine, "create_symbol_index") as create, patch.object(engine, "elasticsearch_client") as es:
            es.options.return_value.search.return_value = {"hits": {"hits": []}}
            engine.bm25_search("http request")
            query = es.options.return_value.search.call_args.kwargs["query"]
            self.assertEqual(query["bool"]["filter"], [{"term": {"is_test": False}}])
            engine.bm25_search("unit test")
            self.assertIn("multi_match", es.options.return_value.search.call_args.kwargs["query"])
        create.assert_not_called()

    def test_semantic_uses_embedding_and_candidate_count(self):
        with patch.object(engine, "create_symbol_index") as create, \
             patch.object(engine, "embed_text", return_value=[.1, .2] * 192) as embed, \
             patch.object(engine, "elasticsearch_client") as es:
            es.options.return_value.search.return_value = {"hits": {"hits": []}}
            engine.semantic_search("q", 40)
        embed.assert_called_once_with("q")
        knn = es.options.return_value.search.call_args.kwargs["knn"]
        self.assertEqual(knn["query_vector"], [.1, .2] * 192)
        self.assertEqual(knn["num_candidates"], 320)
        create.assert_not_called()

    def test_missing_index_does_not_create_empty_index(self):
        with patch.object(engine, "create_symbol_index") as create, \
             patch.object(engine, "elasticsearch_client") as es:
            es.options.return_value.search.side_effect = RuntimeError("index not found")
            with self.assertRaises(RuntimeError):
                engine.bm25_search("q")
        create.assert_not_called()


class ParserTests(unittest.TestCase):
    def test_nul_diff_preserves_unusual_paths_and_copy_destination(self):
        output = "M\0tab\tline\n.py\0R100\0old name.py\0 new\nname.py \0C100\0source.py\0copy.py\0"
        self.assertEqual(parse_git_diff(output, nul=True), [
            {"status": "M", "path": "tab\tline\n.py"},
            {"status": "R", "old_path": "old name.py", "path": " new\nname.py "},
            {"status": "A", "path": "copy.py"}])

    def test_malformed_nul_diff_fails_instead_of_ignoring_records(self):
        for output in ("M\0path", "R100\0old\0", "M\0\0", "U\0path\0", "M\tpath\n"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse_git_diff(output, nul=True)

    def test_plain_github_url_variants_preserve_components(self):
        for url in ("https://github.com/Owner-1/repo_name.v2.git",
                    "http://www.github.com/Owner-1/repo_name.v2/",
                    "https://GITHUB.COM/Owner-1/repo_name.v2.git/"):
            with self.subTest(url=url):
                self.assertEqual(parse_github_url(url), ("Owner-1", "repo_name.v2"))

    def test_ambiguous_url_forms_rejected_before_ingestion_side_effects(self):
        from app.indexer import repository
        urls = ["https://token@github.com/o/r", "https://user:password@github.com/o/r",
                "https://github.com:443/o/r", "https://github.com:bad/o/r",
                "https://github.com/o/r?token=secret", "https://github.com/o/r#main",
                "https://github.com//o/r", "https://github.com/o//r", "https://github.com/o/r//",
                "https://github.com/o/%2e%2e", "https://github.com/o/r%2fname",
                "https://github.com/o/r\\name", "https://github.com/o/r\n",
                " https://github.com/o/r", "https://github.com/o/r\x00",
                "https://github.com/o/r name", "https://github.com/o/répo",
                "https://github.com/o/..git"]
        db = Mock()
        with patch.object(repository.subprocess, "run") as git, patch.object(repository.Path, "mkdir") as mkdir:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(InvalidRepositoryURL):
                    repository.ingest_repository(db, url)
            db.query.assert_not_called()
            git.assert_not_called()
            mkdir.assert_not_called()

    def test_python_symbols(self):
        symbols = parse_python_source("class A:\n    def run(self):\n        return 1\n")
        self.assertEqual([(s.qualified_name, s.kind) for s in symbols], [("A", "class"), ("A.run", "method")])
        self.assertEqual(symbols[1].start_line, 2)

    def test_github_url_validation(self):
        self.assertEqual(parse_github_url("https://github.com/pallets/click.git"), ("pallets", "click"))
        for url in ["file:///tmp/repo", "https://example.com/a/b", "https://github.com/a"]:
            with self.assertRaises(ValueError):
                parse_github_url(url)

    def test_incremental_diff(self):
        self.assertEqual(parse_git_diff("M\ta.py\nR100\tb.py\tc.py\nD\td.py\n"), [
            {"status": "M", "path": "a.py"},
            {"status": "R", "old_path": "b.py", "path": "c.py"},
            {"status": "D", "path": "d.py"},
        ])


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def test_workload_parity(self):
        source = ast.parse(Path("scripts/benchmark_concurrent.py").read_text())
        for node in source.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in {"QUERIES", "TOTAL_REQUESTS", "CONCURRENCY_LEVELS"}:
                    self.assertEqual(getattr(benchmark_api, name), ast.literal_eval(node.value))

    async def test_uncached_rejects_unconfirmed_or_cached_response(self):
        for acknowledged, hit in [(False, False), (True, True), (True, False)]:
            async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers={"X-CodeAtlas-Cache-Bypassed": str(acknowledged).lower()},
                    json={"results": [], "cache_hit": hit, "search_latency_ms": 2, "elasticsearch_latency_ms": 1})
            )) as client:
                result = await benchmark_api.run_request(client, "unchanged", uncached=True)
            self.assertEqual(result.error is None, acknowledged and not hit)

    async def test_uncached_rejects_coalesced_response(self):
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"X-CodeAtlas-Cache-Bypassed": "true"},
                json={"results": [], "cache_hit": False, "cache_coalesced": True,
                      "search_latency_ms": 2, "elasticsearch_latency_ms": 1})
        )) as client:
            result = await benchmark_api.run_request(client, "q", uncached=True)
        self.assertEqual(result.error, "Invalid search response")

    async def test_timeout_and_http_failure_are_counted(self):
        def timeout(request):
            raise httpx.ReadTimeout("timeout", request=request)
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(timeout)) as client:
            result = await benchmark_api.run_request(client, "q")
        summary = benchmark_api.summarize([result], 1, 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["no_response"], 1)
        self.assertIsNone(summary["p95"])
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
            lambda request: httpx.Response(503)
        )) as client:
            failed = await benchmark_api.run_request(client, "q")
        summary = benchmark_api.summarize([result, failed], 1, 1)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["statuses"], {503: 1})

    async def test_workers_bound_concurrency_and_exclude_warmup(self):
        active = peak = measured = 0
        calls = []

        async def handler(request):
            nonlocal active, peak, measured
            if request.url.path == "/health":
                calls.append("health")
                return httpx.Response(200, json={"status": "healthy"})
            self.assertEqual(request.headers["X-CodeAtlas-Benchmark-Bypass"], "true")
            calls.append("search")
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            measured += 1
            return httpx.Response(200, headers={"X-CodeAtlas-Cache-Bypassed": "true"}, json={
                "results": [], "cache_hit": False, "search_latency_ms": 1,
                "elasticsearch_latency_ms": .9,
            })

        client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))
        with patch.object(benchmark_api.httpx, "AsyncClient", return_value=client), redirect_stdout(io.StringIO()):
            summary = await benchmark_api.benchmark(5, uncached=True)
        self.assertEqual(calls[0], "health")
        self.assertEqual(measured, 210)
        self.assertEqual(peak, 5)
        self.assertEqual(summary["total"], 200)
        self.assertEqual(summary["successful"], 200)

    async def test_uncached_preflight_failure_skips_measured_requests(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "healthy"})
            return httpx.Response(403)

        client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))
        with patch.object(benchmark_api.httpx, "AsyncClient", return_value=client), redirect_stdout(io.StringIO()):
            summary = await benchmark_api.benchmark(5, uncached=True)
        self.assertIsNone(summary)
        self.assertEqual(len(calls), 11)


if __name__ == "__main__":
    unittest.main()
