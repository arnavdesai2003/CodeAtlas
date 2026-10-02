"""Direct-service simultaneous misses with an isolated temporary Redis namespace.

Not HTTP, warmed-cache throughput or sustained capacity. No live cache flush,
generation rotation or corpus writes; the namespace is removed on completion.
Can run with archived app sources first on PYTHONPATH for a before/control run.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import platform
from threading import Barrier, Lock
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

from app.search import cache, service
from app.search.embeddings import get_embedding_model
from app.core.config import settings
from scripts.benchmark_api import QUERIES, percentile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--label", default="current")
    args = parser.parse_args()
    if not 1 <= args.workers <= 40 or args.rounds < 1:
        parser.error("workers must be 1–40 and rounds positive")
    prefix = f"codeatlas:diagnostic:missburst:{uuid4().hex}:"
    lock = Lock()
    calls = 0
    original = service.search_code

    def retrieval(*positional, **keyword):
        nonlocal calls
        with lock:
            calls += 1
        return original(*positional, **keyword)

    for query in QUERIES:
        original(query=query, limit=10)
    print(json.dumps({"diagnostic": "direct-service miss bursts, not HTTP/capacity",
        "label": args.label, "loaded_service": service.__file__,
        "platform": platform.platform(), "python": platform.python_version(),
        "device": str(get_embedding_model().device), "torch_num_threads": settings.torch_num_threads,
        "close_search_connections": settings.elasticsearch_close_search_connections,
        "workers": args.workers, "rounds": args.rounds, "queries": len(QUERIES)}), flush=True)
    all_latencies = []
    counts = Counter()
    total_calls = 0
    try:
        with patch.object(cache, "CACHE_PREFIX", prefix), \
             patch.object(cache, "ENTRY_PREFIX", prefix + "entries:"), \
             patch.object(cache, "GENERATION_KEY", prefix + "generation"), \
             patch.object(service, "search_code", retrieval), \
             ThreadPoolExecutor(max_workers=args.workers) as pool:
            for round_number in range(args.rounds):
                for query_number, query in enumerate(QUERIES):
                    cache.invalidate_search_cache(strict=True)
                    released = []
                    barrier = Barrier(args.workers, action=lambda: released.append(perf_counter()))
                    calls = 0

                    def request():
                        barrier.wait(timeout=30)
                        start = perf_counter()
                        result = service.search_with_cache(query=query, limit=10)
                        end = perf_counter()
                        return result, (end - start) * 1000, end

                    futures = [pool.submit(request) for _ in range(args.workers)]
                    responses = [future.result(timeout=120) for future in futures]
                    if not all(result["results"] == responses[0][0]["results"] for result, _, _ in responses):
                        raise RuntimeError("Concurrent result payloads differ; discard this burst.")
                    if not all(result["results"] == cache.get_cached_search(query, 10).results for result, _, _ in responses):
                        raise RuntimeError("Isolated cache did not retain this burst's result.")
                    batch = Counter("hit" if result["cache_hit"] else
                        "coalesced" if result.get("cache_coalesced", False) else "retrieved"
                        for result, _, _ in responses)
                    latencies = [elapsed for _, elapsed, _ in responses]
                    counts.update(batch)
                    all_latencies.extend(latencies)
                    total_calls += calls
                    print(json.dumps({"round": round_number + 1, "query_number": query_number + 1,
                        "successes": len(responses), "engine_calls": calls, "responses": dict(batch),
                        "burst_ms": (max(end for _, _, end in responses) - released[0]) * 1000,
                        "avg_ms": sum(latencies) / len(latencies), "p95_ms": percentile(latencies, .95)}), flush=True)
        summary = {"completed": True, "successes": len(all_latencies), "engine_calls": total_calls,
            "responses": dict(counts), "avg_ms": sum(all_latencies) / len(all_latencies),
            "p95_ms": percentile(all_latencies, .95)}
    finally:
        # Exact keys from this unpredictable namespace only; never FLUSHDB.
        keys = list(cache.redis_client.scan_iter(match=f"{prefix}*", count=100))
        if keys:
            cache.redis_client.delete(*keys)
        if list(cache.redis_client.scan_iter(match=f"{prefix}*", count=100)):
            raise RuntimeError("Temporary namespace cleanup incomplete.")
    print(json.dumps(summary | {"namespace_removed": True}), flush=True)


if __name__ == "__main__":
    main()
