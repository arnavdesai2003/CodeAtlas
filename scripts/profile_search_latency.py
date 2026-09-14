import statistics
from time import perf_counter

from app.core.clients import elasticsearch_client
from app.search.embeddings import embed_text
from app.search.engine import (
    INDEX_NAME,
    _format_hits,
    _min_max_normalize,
    create_symbol_index,
    query_has_test_intent,
)


QUERIES = [
    "send an HTTP GET request",
    "HTTP client timeout handling",
    "parse command line arguments",
    "securely sign data",
    "verify a signed token",
    "escape HTML markup",
    "authentication header",
    "JSON response decoding",
    "request session",
    "HTTP transport",
]


def percentile(values, p):
    ordered = sorted(values)
    index = int((len(ordered) - 1) * p)
    return ordered[index]


def print_stats(name, values):
    print(
        f"{name:<20}"
        f"avg={statistics.mean(values):>8.2f} ms  "
        f"p50={percentile(values, 0.50):>8.2f} ms  "
        f"p95={percentile(values, 0.95):>8.2f} ms"
    )


def bm25_only(query, limit=40):
    lexical_query = {
        "multi_match": {
            "query": query,
            "fields": [
                "qualified_name^5",
                "name^4",
                "path^2",
                "code",
            ],
            "type": "best_fields",
        }
    }

    if query_has_test_intent(query):
        final_query = lexical_query
    else:
        final_query = {
            "bool": {
                "must": [lexical_query],
                "filter": [
                    {
                        "term": {
                            "is_test": False
                        }
                    }
                ],
            }
        }

    response = (
        elasticsearch_client
        .options(request_timeout=60)
        .search(
            index=INDEX_NAME,
            size=limit,
            query=final_query,
        )
    )

    return _format_hits(
        response["hits"]["hits"]
    )


def vector_only(
    query,
    query_vector,
    limit=40,
):
    knn_query = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": limit,
        "num_candidates": max(
            limit * 8,
            100,
        ),
    }

    if not query_has_test_intent(query):
        knn_query["filter"] = {
            "term": {
                "is_test": False
            }
        }

    response = (
        elasticsearch_client
        .options(request_timeout=60)
        .search(
            index=INDEX_NAME,
            size=limit,
            knn=knn_query,
        )
    )

    return _format_hits(
        response["hits"]["hits"]
    )


def fuse(
    bm25_results,
    semantic_results,
    semantic_weight=0.60,
    limit=10,
):
    bm25_scores = _min_max_normalize(
        [float(r["score"] or 0) for r in bm25_results]
    )

    semantic_scores = _min_max_normalize(
        [float(r["score"] or 0) for r in semantic_results]
    )

    candidates = {}

    for result, score in zip(
        bm25_results,
        bm25_scores,
    ):
        key = (
            result["repository"],
            result["path"],
            result["start_line"],
        )

        candidates[key] = {
            **result,
            "bm25_score": score,
            "semantic_score": 0.0,
        }

    for result, score in zip(
        semantic_results,
        semantic_scores,
    ):
        key = (
            result["repository"],
            result["path"],
            result["start_line"],
        )

        if key not in candidates:
            candidates[key] = {
                **result,
                "bm25_score": 0.0,
                "semantic_score": score,
            }
        else:
            candidates[key][
                "semantic_score"
            ] = score

    bm25_weight = 1.0 - semantic_weight

    for candidate in candidates.values():
        candidate["score"] = (
            semantic_weight
            * candidate["semantic_score"]
            + bm25_weight
            * candidate["bm25_score"]
        )

    return sorted(
        candidates.values(),
        key=lambda item: item["score"],
        reverse=True,
    )[:limit]


def main():
    create_symbol_index()

    # Warm model + Elasticsearch.
    for _ in range(2):
        vector = embed_text(QUERIES[0])
        bm25_only(QUERIES[0])
        vector_only(
            QUERIES[0],
            vector,
        )

    embedding_times = []
    bm25_times = []
    vector_times = []
    fusion_times = []
    total_times = []

    for query in QUERIES * 4:
        total_start = perf_counter()

        start = perf_counter()
        vector = embed_text(query)
        embedding_times.append(
            (perf_counter() - start) * 1000
        )

        start = perf_counter()
        bm25_results = bm25_only(query)
        bm25_times.append(
            (perf_counter() - start) * 1000
        )

        start = perf_counter()
        semantic_results = vector_only(
            query,
            vector,
        )
        vector_times.append(
            (perf_counter() - start) * 1000
        )

        start = perf_counter()
        fuse(
            bm25_results,
            semantic_results,
        )
        fusion_times.append(
            (perf_counter() - start) * 1000
        )

        total_times.append(
            (perf_counter() - total_start) * 1000
        )

    print()
    print("CODEATLAS SEARCH LATENCY BREAKDOWN")
    print("=" * 85)

    print_stats(
        "Query embedding",
        embedding_times,
    )

    print_stats(
        "BM25 Elasticsearch",
        bm25_times,
    )

    print_stats(
        "Vector kNN",
        vector_times,
    )

    print_stats(
        "Score fusion",
        fusion_times,
    )

    print_stats(
        "TOTAL",
        total_times,
    )


if __name__ == "__main__":
    main()