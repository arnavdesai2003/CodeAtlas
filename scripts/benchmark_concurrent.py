import concurrent.futures
import statistics
import time

from app.search.engine import hybrid_search


QUERIES = [
    "function that computes gradients during backpropagation",
    "parse command line arguments",
    "send an HTTP request",
    "serialize an object into signed data",
    "raise an exception for HTTP failure",
    "build authentication header",
    "parse URL",
    "create request headers",
    "decode response content",
    "validate command parameters",
]

TOTAL_REQUESTS = 200
CONCURRENCY_LEVELS = [1, 5, 10, 20]


def percentile(values, p):
    values = sorted(values)
    index = int((len(values) - 1) * p)
    return values[index]


def run_request(i):
    query = QUERIES[i % len(QUERIES)]

    start = time.perf_counter()

    hybrid_search(
        query=query,
        limit=10,
    )

    return (time.perf_counter() - start) * 1000


def benchmark(workers):
    # Warm search infrastructure before measurement.
    for query in QUERIES:
        hybrid_search(query=query, limit=10)

    latencies = []

    start = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = [
            executor.submit(run_request, i)
            for i in range(TOTAL_REQUESTS)
        ]

        for future in concurrent.futures.as_completed(futures):
            latencies.append(future.result())

    elapsed = time.perf_counter() - start

    throughput = TOTAL_REQUESTS / elapsed

    print()
    print("=" * 60)
    print(f"CONCURRENCY: {workers}")
    print("=" * 60)
    print(f"Requests    : {TOTAL_REQUESTS}")
    print(f"Throughput  : {throughput:.2f} req/s")
    print(f"Average     : {statistics.mean(latencies):.3f} ms")
    print(f"Median      : {statistics.median(latencies):.3f} ms")
    print(f"p50         : {percentile(latencies, 0.50):.3f} ms")
    print(f"p95         : {percentile(latencies, 0.95):.3f} ms")
    print(f"p99         : {percentile(latencies, 0.99):.3f} ms")
    print(f"Min         : {min(latencies):.3f} ms")
    print(f"Max         : {max(latencies):.3f} ms")


def main():
    print("=" * 60)
    print("CODEATLAS CONCURRENT SEARCH BENCHMARK")
    print("=" * 60)

    for workers in CONCURRENCY_LEVELS:
        benchmark(workers)


if __name__ == "__main__":
    main()