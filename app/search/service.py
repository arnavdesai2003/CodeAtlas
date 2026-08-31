from time import perf_counter

from app.search.cache import (
    get_cached_search,
    set_cached_search,
)
from app.search.engine import search_code


def search_with_cache(
    query: str,
    limit: int,
) -> dict:
    total_start = perf_counter()

    cached_results = get_cached_search(
        query=query,
        limit=limit,
    )

    if cached_results is not None:
        total_latency_ms = (
            perf_counter() - total_start
        ) * 1000

        return {
            "results": cached_results,
            "cache_hit": True,
            "search_latency_ms": round(
                total_latency_ms,
                3,
            ),
        }

    elasticsearch_start = perf_counter()

    results = search_code(
        query=query,
        limit=limit,
    )

    elasticsearch_latency_ms = (
        perf_counter() - elasticsearch_start
    ) * 1000

    set_cached_search(
        query=query,
        limit=limit,
        results=results,
    )

    total_latency_ms = (
        perf_counter() - total_start
    ) * 1000

    return {
        "results": results,
        "cache_hit": False,
        "elasticsearch_latency_ms": round(
            elasticsearch_latency_ms,
            3,
        ),
        "search_latency_ms": round(
            total_latency_ms,
            3,
        ),
    }