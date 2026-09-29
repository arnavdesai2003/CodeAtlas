"""Deterministic cache races: invalidate inside an outstanding search."""

import fnmatch
import os
import unittest
from unittest.mock import patch

os.environ.update(
    DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
    ELASTICSEARCH_URL="http://127.0.0.1:9200",
    REDIS_URL="redis://127.0.0.1:6379/15",
    HF_HUB_OFFLINE="1",
)

from app.search import cache, service


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value

    def getset(self, key, value):
        previous = self.values.get(key)
        self.values[key] = value
        return previous

    def eval(self, script, numkeys, *args):
        # Models Redis atomic execution for deterministic service interleavings.
        # The real Lua scripts are additionally verified against local Redis.
        if script == cache._READ_SCRIPT:
            key, candidate, prefix, digest = args
            generation = self.values.setdefault(key, candidate)
            return [generation, self.values.get(f"{prefix}{generation}:{digest}")]
        if script == cache._WRITE_SCRIPT:
            key, entry, generation, ttl, value = args
            if self.values.get(key) != generation:
                return 0
            self.setex(entry, ttl, value)
            return 1
        raise AssertionError("Unexpected Redis script")

    def scan_iter(self, match, **kwargs):
        return iter([key for key in self.values if fnmatch.fnmatchcase(key, match)])

    def delete(self, *keys):
        count = sum(key in self.values for key in keys)
        for key in keys:
            self.values.pop(key, None)
        return count


class CacheGenerationTests(unittest.TestCase):
    def test_stale_writer_cannot_overwrite_a_new_generation(self):
        redis = MemoryRedis()
        with patch.object(cache, "redis_client", redis):
            old = cache.get_cached_search("q", 10)
            cache.invalidate_search_cache(strict=True)
            new = cache.get_cached_search("q", 10)
            self.assertNotEqual(old.generation, new.generation)
            self.assertTrue(cache.set_cached_search("q", 10, [{"name": "new"}], generation=new.generation))
            self.assertFalse(cache.set_cached_search("q", 10, [{"name": "old"}], generation=old.generation))
            self.assertEqual(cache.get_cached_search("q", 10).results, [{"name": "new"}])

    def test_rotation_still_invalidates_when_cleanup_fails(self):
        redis = MemoryRedis()
        with patch.object(cache, "redis_client", redis):
            old = cache.get_cached_search("q", 10)
            cache.set_cached_search("q", 10, [{"name": "old"}], generation=old.generation)
            with patch.object(redis, "scan_iter", side_effect=ConnectionError("cleanup unavailable")):
                self.assertEqual(cache.invalidate_search_cache(strict=True), 0)
            self.assertIsNone(cache.get_cached_search("q", 10).results)
            self.assertIn(cache.build_cache_key("q", 10, generation=old.generation), redis.values)

    def test_evicted_generation_is_not_reused(self):
        redis = MemoryRedis()
        with patch.object(cache, "redis_client", redis):
            old = cache.get_cached_search("q", 10)
            cache.set_cached_search("q", 10, [{"name": "old"}], generation=old.generation)
            redis.delete(cache.GENERATION_KEY)
            new = cache.get_cached_search("q", 10)
            self.assertIsNone(new.results)
            self.assertNotEqual(old.generation, new.generation)
            self.assertFalse(cache.set_cached_search("q", 10, [], generation=old.generation))

    def test_lookup_outage_cannot_cache_after_redis_recovers(self):
        redis = MemoryRedis()
        with patch.object(cache, "redis_client", redis), \
             patch.object(redis, "eval", side_effect=ConnectionError("offline")), \
             patch.object(service, "set_cached_search") as write, \
             patch.object(service, "search_code", return_value=[{"name": "result"}]):
            result = service.search_with_cache("q", 10)
        write.assert_not_called()
        self.assertEqual(result["results"], [{"name": "result"}])
        self.assertFalse(result["cache_hit"])

    def test_unknown_generation_never_attempts_a_write(self):
        redis = MemoryRedis()
        with patch.object(cache, "redis_client", redis), patch.object(redis, "eval") as execute:
            self.assertFalse(cache.set_cached_search("q", 10, [], generation=None))
        execute.assert_not_called()

    def test_cleanup_only_deletes_previous_generation(self):
        redis = MemoryRedis()
        with patch.object(cache, "redis_client", redis):
            old = cache.get_cached_search("q", 10)
            for i in range(300):
                cache.set_cached_search(f"query {i}", 10, [], generation=old.generation)
            redis.values["unrelated"] = "keep"
            self.assertEqual(cache.invalidate_search_cache(strict=True), 300)
            self.assertIn(cache.GENERATION_KEY, redis.values)
            self.assertEqual(redis.values["unrelated"], "keep")
            current = cache.get_cached_search("q", 10)
            self.assertTrue(cache.set_cached_search("q", 10, [], generation=current.generation))
            self.assertEqual(cache.get_cached_search("q", 10).results, [])

    def test_corrupt_shape_is_a_miss_with_a_safe_generation(self):
        redis = MemoryRedis()
        with patch.object(cache, "redis_client", redis):
            lookup = cache.get_cached_search("q", 10)
            key = cache.build_cache_key("q", 10, generation=lookup.generation)
            for value in ['{"unexpected": "object"}', '["not a result"]', 'null']:
                redis.values[key] = value
                result = cache.get_cached_search("q", 10)
                self.assertIsNone(result.results)
                self.assertEqual(result.generation, lookup.generation)

    def test_inflight_search_cannot_refill_cache_after_invalidation(self):
        redis = MemoryRedis()
        stale = [{"name": "old"}]
        fresh = [{"name": "new"}]

        def retrieval(query, limit):
            cache.invalidate_search_cache(strict=True)
            return stale

        with patch.object(cache, "redis_client", redis), \
             patch.object(service, "search_code", side_effect=retrieval) as search:
            first = service.search_with_cache("q", 10)
            search.side_effect = None
            search.return_value = fresh
            second = service.search_with_cache("q", 10)
            third = service.search_with_cache("q", 10)
        # The already-running caller may receive its old result; future callers
        # must retrieve fresh data instead of inheriting that stale cache fill.
        self.assertEqual(first["results"], stale)
        self.assertEqual(second["results"], fresh)
        self.assertFalse(second["cache_hit"])
        self.assertTrue(third["cache_hit"])
        self.assertEqual(search.call_count, 2)


if __name__ == "__main__":
    unittest.main()
