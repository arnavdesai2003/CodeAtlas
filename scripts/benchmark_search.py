import statistics
from time import perf_counter

from app.search.service import search_with_cache


QUERIES = [
    "backward gradient",
    "zero gradient",
    "neural network layer",
    "relu activation",
    "parameters",
]


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = int(p * (len(ordered) - 1))
    return ordered[index]


def main():
    # Warm cache.
    for query in QUERIES:
        search_with_cache(query=query, limit=5)

    latencies = []

    for i in range(100):
        query = QUERIES[i % len(QUERIES)]

        start = perf_counter()

        search_with_cache(
            query=query,
            limit=5,
        )

        latencies.append(
            (perf_counter() - start) * 1000
        )

    print("\nCodeAtlas Cached Search Benchmark")
    print("=" * 40)
    print(f"Requests: {len(latencies)}")
    print(f"Average: {statistics.mean(latencies):.3f} ms")
    print(f"Median: {statistics.median(latencies):.3f} ms")
    print(f"p95: {percentile(latencies, 0.95):.3f} ms")
    print(f"p99: {percentile(latencies, 0.99):.3f} ms")
    print(f"Min: {min(latencies):.3f} ms")
    print(f"Max: {max(latencies):.3f} ms")


if __name__ == "__main__":
    main()