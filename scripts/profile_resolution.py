"""Read-only request-sequencing diagnostic; not HTTP or capacity benchmarking.

Run without concurrent publication/indexing for interpretable comparisons.
Each normal hybrid call still resolves and pins its own concrete generation.
"""

import argparse
import json
import platform
import statistics
from time import perf_counter, sleep

from elasticsearch import Elasticsearch

from app.core.config import settings
from app.search import engine
from app.search.embeddings import get_embedding_model
from app.search.indexes import resolve_active_index
from scripts.benchmark_api import QUERIES, percentile


def measure(predecessor, request, *, samples, warmups):
    durations = []
    for i in range(warmups + samples):
        predecessor(QUERIES[i % len(QUERIES)])
        start = perf_counter()
        request()
        elapsed = (perf_counter() - start) * 1000
        if i >= warmups:
            durations.append(elapsed)
    return {"successes": len(durations), "avg_ms": statistics.mean(durations),
            "p95_ms": percentile(durations, .95)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--warmups", type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 0:
        parser.error("samples must be positive and warmups nonnegative")

    client = engine.elasticsearch_client
    index = engine.resolve_search_index()
    identity = client.indices.get_settings(index=index)[index]["settings"]["index"]["uuid"]
    count = client.count(index=index)["count"]
    for query in QUERIES:
        engine.hybrid_search(query, 10)
    print(json.dumps({"diagnostic": "request sequencing, not HTTP latency",
                      "machine": platform.machine(), "platform": platform.platform(),
                      "python": platform.python_version(),
                      "embedding_device": str(get_embedding_model().device),
                      "torch_num_threads_setting": settings.torch_num_threads,
                      "index": index, "index_uuid": identity, "documents": count,
                      "samples_per_block": args.samples, "excluded_warmups": args.warmups}),
          flush=True)

    # A separate pool is a diagnostic control, never an application routing cache.
    separate = Elasticsearch(settings.elasticsearch_url, request_timeout=60,
                             retry_on_timeout=True, max_retries=3)
    try:
        predecessors = [
            ("none", lambda query: None),
            ("idle_20_ms", lambda query: sleep(.020)),
            ("embedding", engine.embed_text),
            ("bm25", lambda query: engine.bm25_search(query, 40, index_name=index)),
            ("hybrid", lambda query: engine.hybrid_search(query, 10)),
            ("none_control", lambda query: None),
        ]
        for label, predecessor in predecessors:
            result = measure(predecessor, engine.resolve_search_index, **vars(args))
            print(json.dumps({"predecessor": label, "request": "alias", **result}), flush=True)
        bm25 = predecessors[3][1]
        for label, request in [
            ("separate_pool_alias", lambda: resolve_active_index(
                separate, alias_name=engine.SEARCH_ALIAS, legacy_name=engine.INDEX_NAME)),
            ("info", client.info),
            ("count", lambda: client.count(index=index)),
            ("alias_control", engine.resolve_search_index),
        ]:
            result = measure(bm25, request, **vars(args))
            print(json.dumps({"predecessor": "bm25", "request": label, **result}), flush=True)
        final_index = engine.resolve_search_index()
        final_uuid = client.indices.get_settings(index=index)[index]["settings"]["index"]["uuid"]
        if final_index != index or final_uuid != identity or client.count(index=index)["count"] != count:
            raise RuntimeError("Index routing, identity or count changed; discard this comparison.")
        print(json.dumps({"completed": True, "routing_identity_count_unchanged": True}), flush=True)
    finally:
        separate.close()


if __name__ == "__main__":
    # Exceptions remain visible and cause a nonzero exit; failed blocks are never
    # reported as successful samples. Cache is neither read nor written.
    main()
