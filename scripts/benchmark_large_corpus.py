import statistics
from time import perf_counter

from app.search.cache import invalidate_search_cache
from app.search.engine import hybrid_search
from app.search.service import search_with_cache


QUERIES = [
    "send an HTTP GET request",
    "HTTP client timeout handling",
    "parse command line arguments",
    "command line option validation",
    "securely sign data",
    "verify a signed token",
    "escape HTML markup",
    "construct HTTP request headers",
    "cookie handling",
    "redirect HTTP response",
    "URL parameter encoding",
    "async HTTP request",
    "HTTP connection pooling",
    "raise exception for bad response",
    "parse command arguments",
    "format terminal output",
    "authentication header",
    "JSON response decoding",
    "request session",
    "HTTP transport",
]


def percentile(
    values: list[float],
    p: float,
) -> float:
    if not values:
        return 0.0

    ordered = sorted(values)

    index = int(
        (len(ordered) - 1) * p
    )

    return ordered[index]


def print_metrics(
    title: str,
    latencies: list[float],
) -> None:
    print()
    print(title)
    print("=" * 60)

    print(
        f"Requests : {len(latencies)}"
    )

    print(
        f"Average  : "
        f"{statistics.mean(latencies):.3f} ms"
    )

    print(
        f"Median   : "
        f"{statistics.median(latencies):.3f} ms"
    )

    print(
        f"p50      : "
        f"{percentile(latencies, 0.50):.3f} ms"
    )

    print(
        f"p95      : "
        f"{percentile(latencies, 0.95):.3f} ms"
    )

    print(
        f"p99      : "
        f"{percentile(latencies, 0.99):.3f} ms"
    )

    print(
        f"Min      : "
        f"{min(latencies):.3f} ms"
    )

    print(
        f"Max      : "
        f"{max(latencies):.3f} ms"
    )


def benchmark_uncached() -> list[float]:
    """
    Benchmark Elasticsearch hybrid retrieval directly.

    Redis is bypassed completely.
    """

    print()
    print("Warming embedding model and Elasticsearch...")

    hybrid_search(
        query=QUERIES[0],
        limit=10,
    )

    latencies = []

    for index in range(40):
        query = QUERIES[
            index % len(QUERIES)
        ]

        start = perf_counter()

        hybrid_search(
            query=query,
            limit=10,
        )

        elapsed_ms = (
            perf_counter() - start
        ) * 1000

        latencies.append(
            elapsed_ms
        )

        print(
            f"Uncached "
            f"{index + 1:>3}/40 "
            f"{elapsed_ms:>9.3f} ms",
            flush=True,
        )

    return latencies


def benchmark_cached() -> list[float]:
    """
    Benchmark Redis-served searches.
    """

    invalidate_search_cache()

    # Populate cache.
    for query in QUERIES:
        search_with_cache(
            query=query,
            limit=10,
        )

    latencies = []

    for index in range(100):
        query = QUERIES[
            index % len(QUERIES)
        ]

        start = perf_counter()

        result = search_with_cache(
            query=query,
            limit=10,
        )

        elapsed_ms = (
            perf_counter() - start
        ) * 1000

        if not result["cache_hit"]:
            print(
                "WARNING: expected cache hit "
                f"for query: {query}"
            )

        latencies.append(
            elapsed_ms
        )

    return latencies


def benchmark_mixed() -> tuple[
    list[float],
    int,
    int,
]:
    """
    Simulate a simple mixed workload.

    Each query's first request is a miss.
    Repeated requests become hits.
    """

    invalidate_search_cache()

    latencies = []

    cache_hits = 0
    cache_misses = 0

    workload = []

    for query in QUERIES:
        # First access = miss.
        workload.append(query)

        # Four subsequent accesses = hits.
        workload.extend(
            [query] * 4
        )

    for query in workload:
        start = perf_counter()

        result = search_with_cache(
            query=query,
            limit=10,
        )

        elapsed_ms = (
            perf_counter() - start
        ) * 1000

        latencies.append(
            elapsed_ms
        )

        if result["cache_hit"]:
            cache_hits += 1
        else:
            cache_misses += 1

    return (
        latencies,
        cache_hits,
        cache_misses,
    )


def main():
    print()
    print("=" * 60)
    print("CODEATLAS LARGE-CORPUS BENCHMARK")
    print("=" * 60)

    uncached = benchmark_uncached()

    print_metrics(
        "UNCACHED HYBRID SEARCH",
        uncached,
    )

    cached = benchmark_cached()

    print_metrics(
        "REDIS CACHED SEARCH",
        cached,
    )

    (
        mixed,
        cache_hits,
        cache_misses,
    ) = benchmark_mixed()

    print_metrics(
        "MIXED WORKLOAD",
        mixed,
    )

    total = (
        cache_hits + cache_misses
    )

    hit_rate = (
        cache_hits / total
        if total
        else 0
    )

    print()
    print("CACHE STATISTICS")
    print("=" * 60)
    print(
        f"Hits      : {cache_hits}"
    )
    print(
        f"Misses    : {cache_misses}"
    )
    print(
        f"Hit rate  : {hit_rate:.1%}"
    )


if __name__ == "__main__":
    main()