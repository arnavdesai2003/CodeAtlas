"""Instrument concurrent direct retrieval without changing ranking or queries.

Diagnostic only: overlapping stage times must not be summed. Instrumentation
adds overhead, so use benchmark_concurrent for authoritative engine throughput.
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import wraps
import statistics
from threading import Lock
from time import perf_counter
from unittest.mock import patch

from elasticsearch import Elasticsearch

from app.search import engine
from app.search.embeddings import get_embedding_model
from scripts.benchmark_api import CONCURRENCY_LEVELS, QUERIES, TOTAL_REQUESTS, percentile


def main():
    for query in QUERIES:
        engine.hybrid_search(query, 10)
    print(f"Embedding device: {get_embedding_model().device}", flush=True)
    print("Concurrent direct-engine diagnostic; overlapping stages, not HTTP latency.")
    for workers in CONCURRENCY_LEVELS:
        timings = defaultdict(list)
        lock = Lock()

        def timed(function, label):
            @wraps(function)
            def wrapper(*args, **kwargs):
                start = perf_counter()
                try:
                    return function(*args, **kwargs)
                finally:
                    elapsed = (perf_counter() - start) * 1000
                    key = label(kwargs) if callable(label) else label
                    with lock:
                        timings[key].append(elapsed)
            return wrapper

        with ExitStack() as stack:
            for name, label in [
                ("embed_text", "Embedding"),
                ("create_symbol_index", "Index existence check"),
                ("bm25_search", "BM25 branch"),
                ("semantic_search", "Semantic branch"),
            ]:
                stack.enter_context(patch.object(engine, name, timed(getattr(engine, name), label)))
            stack.enter_context(patch.object(
                Elasticsearch, "search", timed(
                    Elasticsearch.search,
                    lambda kwargs: "Vector ES request" if "knn" in kwargs else "BM25 ES request",
                )
            ))
            search = timed(engine.hybrid_search, "Full hybrid")
            start = perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda i: search(QUERIES[i % len(QUERIES)], 10), range(TOTAL_REQUESTS)))
            elapsed = perf_counter() - start

        print(f"\nWorkers={workers}, requests={TOTAL_REQUESTS}, diagnostic req/s={TOTAL_REQUESTS / elapsed:.2f}")
        for label in ["Embedding", "Index existence check", "BM25 ES request", "Vector ES request",
                      "BM25 branch", "Semantic branch", "Full hybrid"]:
            values = timings[label]
            print(f"{label:<24} n={len(values):>3} avg={statistics.mean(values):>9.3f} ms "
                  f"p95={percentile(values, .95):>9.3f} ms", flush=True)


if __name__ == "__main__":
    main()
