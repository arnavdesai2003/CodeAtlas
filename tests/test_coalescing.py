"""Deterministic service concurrency; no models, network or timing sleeps."""

import os
os.environ.update(DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
    ELASTICSEARCH_URL="http://127.0.0.1:9200", REDIS_URL="redis://127.0.0.1:6379/15", HF_HUB_OFFLINE="1")

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import patch

from app.search import service
from app.search.cache import CacheLookup
from app.search.coalescing import MissFlights


class ObservedFlights(MissFlights):
    def __init__(self, followers=1, **kwargs):
        super().__init__(**kwargs)
        self.joined = Event()
        self.expected = followers
        self.count = 0
        self.observation_lock = Lock()

    def acquire(self, key):
        future, leader = super().acquire(key)
        if future is not None and not leader:
            with self.observation_lock:
                self.count += 1
                if self.count == self.expected:
                    self.joined.set()
        return future, leader


class CoalescingTests(unittest.TestCase):
    def setUp(self):
        self.flights = ObservedFlights(wait_seconds=5)
        self.release = Event()
        self.started = Event()
        self.addCleanup(self.release.set)
        self.registry = patch.object(service, "_miss_flights", self.flights)
        self.registry.start()
        self.addCleanup(self.registry.stop)
        reader = patch.object(service, "get_cached_search", return_value=CacheLookup(generation="g"))
        writer = patch.object(service, "set_cached_search", return_value=True)
        self.read = reader.start()
        self.write = writer.start()
        self.addCleanup(reader.stop)
        self.addCleanup(writer.stop)

    def blocked(self, query, limit):
        self.started.set()
        if not self.release.wait(3):
            raise AssertionError("test release not signalled")
        return [{"name": "result", "nested": {"tags": ["x"]}}]

    def grouped(self, retrieval, workers=2):
        self.flights.expected = workers - 1
        with patch.object(service, "search_code", side_effect=retrieval) as engine, \
             ThreadPoolExecutor(max_workers=workers) as pool:
            leader = pool.submit(service.search_with_cache, "q", 10)
            self.assertTrue(self.started.wait(2))
            followers = [pool.submit(service.search_with_cache, "q", 10) for _ in range(workers - 1)]
            try:
                self.assertTrue(self.flights.joined.wait(2))
            finally:
                self.release.set()
            return engine, [leader, *followers]

    def test_same_key_shares_one_engine_call_and_distinguishes_waiters(self):
        engine, futures = self.grouped(self.blocked, workers=8)
        responses = [future.result() for future in futures]
        self.assertEqual(engine.call_count, 1)
        self.assertEqual(sum(r["cache_coalesced"] for r in responses), 7)
        self.assertFalse(any(r["cache_hit"] for r in responses))
        self.assertIn("elasticsearch_latency_ms", responses[0])
        for result in responses[1:]:
            self.assertNotIn("elasticsearch_latency_ms", result)
            self.assertGreaterEqual(result["search_latency_ms"], result["coalescing_wait_ms"])
        self.assertEqual(self.write.call_count, 1)
        self.assertEqual(self.flights._pending, {})

    def test_result_objects_are_independent(self):
        _, futures = self.grouped(self.blocked, workers=3)
        responses = [f.result() for f in futures]
        responses[0]["results"][0]["nested"]["tags"].append("leader mutation")
        responses[1]["results"][0]["nested"]["tags"].append("follower mutation")
        self.assertEqual(responses[2]["results"][0]["nested"]["tags"], ["x"])

    def test_empty_results_share_successfully(self):
        def empty(query, limit):
            self.blocked(query, limit)
            return []
        engine, futures = self.grouped(empty)
        self.assertEqual(engine.call_count, 1)
        self.assertEqual([f.result()["results"] for f in futures], [[], []])

    def test_errors_release_waiters_and_allow_retry_including_timeout_errors(self):
        for error_type in (RuntimeError, TimeoutError):
            with self.subTest(error=error_type):
                self.started.clear(); self.release.clear(); self.flights.joined.clear(); self.flights.count = 0
                def fail(query, limit):
                    self.blocked(query, limit)
                    raise error_type("engine failed")
                engine, futures = self.grouped(fail)
                for future in futures:
                    with self.assertRaisesRegex(error_type, "engine failed"):
                        future.result()
                self.assertEqual(engine.call_count, 1)
                self.assertEqual(self.flights._pending, {})
                with patch.object(service, "search_code", return_value=[]):
                    self.assertFalse(service.search_with_cache("q", 10)["cache_coalesced"])

    def test_wait_timeout_falls_back_without_removing_active_leader(self):
        self.flights.wait_seconds = 0
        counter = 0
        def retrieve(query, limit):
            nonlocal counter
            counter += 1
            return self.blocked(query, limit) if counter == 1 else []
        with patch.object(service, "search_code", side_effect=retrieve) as engine, ThreadPoolExecutor(1) as pool:
            leader = pool.submit(service.search_with_cache, "q", 10)
            try:
                self.assertTrue(self.started.wait(2))
                follower = service.search_with_cache("q", 10)
                self.assertFalse(follower["cache_coalesced"])
                self.assertIn("elasticsearch_latency_ms", follower)
                self.assertEqual(len(self.flights._pending), 1)
            finally:
                self.release.set()
            leader.result()
            self.assertEqual(engine.call_count, 2)
        self.assertEqual(self.flights._pending, {})

    def test_new_generation_does_not_join_old_work_or_accept_old_fill(self):
        generation = ["old"]
        stored = {}
        self.read.side_effect = lambda **kwargs: CacheLookup(stored.get(generation[0]), generation[0])
        def write(**kwargs):
            if kwargs["generation"] != generation[0]:
                return False
            stored[generation[0]] = kwargs["results"]
            return True
        self.write.side_effect = write
        def retrieve(query, limit):
            return self.blocked(query, limit) if generation[0] == "old" else [{"name": "fresh"}]
        with patch.object(service, "search_code", side_effect=retrieve) as engine, ThreadPoolExecutor(2) as pool:
            old = pool.submit(service.search_with_cache, "q", 10)
            try:
                self.assertTrue(self.started.wait(2))
                old_follower = pool.submit(service.search_with_cache, "q", 10)
                self.assertTrue(self.flights.joined.wait(2))
                generation[0] = "new"
                fresh = service.search_with_cache("q", 10)
                self.assertEqual(fresh["results"], [{"name": "fresh"}])
                self.assertFalse(fresh["cache_coalesced"])
            finally:
                self.release.set()
            self.assertEqual(old.result()["results"], old_follower.result()["results"])
            self.assertEqual(engine.call_count, 2)
            self.assertEqual(stored, {"new": [{"name": "fresh"}]})
            self.assertTrue(service.search_with_cache("q", 10)["cache_hit"])

    def test_unknown_generation_and_bypass_never_join(self):
        for bypass in (False, True):
            with self.subTest(bypass=bypass):
                self.read.return_value = CacheLookup()
                barrier = Barrier(4)
                def retrieve(query, limit):
                    barrier.wait(timeout=3)
                    return []
                with patch.object(self.flights, "acquire", side_effect=AssertionError("unexpected join")), \
                     patch.object(service, "search_code", side_effect=retrieve) as engine, ThreadPoolExecutor(4) as pool:
                    futures = [pool.submit(service.search_with_cache, "q", 10, bypass_cache=bypass) for _ in range(4)]
                    self.assertTrue(all(not f.result()["cache_coalesced"] for f in futures))
                    self.assertEqual(engine.call_count, 4)
        self.write.assert_not_called()

    def test_exact_query_and_limit_separate_work(self):
        inputs = [("q", 10), ("Q", 10), (" q ", 10), ("q", 20)]
        barrier = Barrier(4)
        def retrieve(query, limit):
            barrier.wait(timeout=3)
            return [{"query": query, "limit": limit}]
        with patch.object(service, "search_code", side_effect=retrieve) as engine, ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(service.search_with_cache, q, limit) for q, limit in inputs]
            self.assertEqual([f.result()["results"][0] for f in futures],
                             [{"query": q, "limit": limit} for q, limit in inputs])
            self.assertEqual(engine.call_count, 4)

    def test_saturation_keeps_existing_joinable_and_other_queries_independent(self):
        self.flights.max_keys = 1
        with patch.object(service, "search_code", side_effect=lambda query, limit:
                          self.blocked(query, limit) if query == "q" else []) as engine, ThreadPoolExecutor(2) as pool:
            leader = pool.submit(service.search_with_cache, "q", 10)
            try:
                self.assertTrue(self.started.wait(2))
                self.assertFalse(service.search_with_cache("other", 10)["cache_coalesced"])
                follower = pool.submit(service.search_with_cache, "q", 10)
                self.assertTrue(self.flights.joined.wait(2))
                self.assertEqual(len(self.flights._pending), 1)
            finally:
                self.release.set()
            self.assertEqual(leader.result()["results"], follower.result()["results"])
            self.assertEqual(engine.call_count, 2)

    def test_second_read_avoids_late_duplicate_fill(self):
        self.read.side_effect = [CacheLookup(generation="g"), CacheLookup([], "g")]
        with patch.object(service, "search_code") as engine:
            self.assertTrue(service.search_with_cache("q", 10)["cache_hit"])
            engine.assert_not_called()
        self.assertEqual(self.flights._pending, {})

    def test_second_read_never_rebinds_old_work_to_new_generation(self):
        self.read.side_effect = [CacheLookup(generation="old"), CacheLookup([], "new")]
        with patch.object(service, "search_code", return_value=[{"name": "retrieved"}]):
            result = service.search_with_cache("q", 10)
        self.assertFalse(result["cache_hit"])
        self.write.assert_called_once_with(query="q", limit=10, results=[{"name": "retrieved"}], generation="old")

    def test_failed_cache_write_does_not_prevent_sharing_or_retain_results(self):
        self.write.return_value = False
        engine, futures = self.grouped(self.blocked)
        self.assertTrue(futures[1].result()["cache_coalesced"])
        self.assertEqual(engine.call_count, 1)
        with patch.object(service, "search_code", return_value=[]) as retry:
            service.search_with_cache("q", 10)
            retry.assert_called_once()


if __name__ == "__main__":
    unittest.main()
