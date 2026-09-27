"""Warm-cache HTTP benchmark. Run with: python -m scripts.benchmark_api.

Client latency covers sending the request and reading the complete response,
excluding local worker-queue wait and JSON validation. Server timings are
reported separately. Health checks and warm-up are never measured.
"""

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
import math
import statistics
from time import perf_counter

import httpx


BASE_URL = "http://127.0.0.1:8000"
TOTAL_REQUESTS = 200
CONCURRENCY_LEVELS = [1, 5, 10, 20]
SEARCH_LIMIT = 10

# Kept identical to benchmark_concurrent.py without importing the search engine.
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


@dataclass
class RequestResult:
    latency_ms: float
    status_code: int | None = None
    error: str | None = None
    cache_hit: bool | None = None
    search_ms: float | None = None
    elasticsearch_ms: float | None = None


def valid_timing(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


async def run_request(client, query, uncached=False):
    start = perf_counter()
    try:
        response = await client.post(
            "/search", json={"query": query, "limit": SEARCH_LIMIT},
            headers={"X-CodeAtlas-Benchmark-Bypass": "true"} if uncached else {},
        )
    except httpx.RequestError as exc:
        return RequestResult(
            latency_ms=(perf_counter() - start) * 1000,
            error=type(exc).__name__,
        )

    result = RequestResult(
        latency_ms=(perf_counter() - start) * 1000,
        status_code=response.status_code,
    )
    if response.status_code != 200:
        result.error = f"HTTP {response.status_code}"
        return result

    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        if not isinstance(payload.get("results"), list):
            raise ValueError("Missing results list")
        if not isinstance(payload.get("cache_hit"), bool):
            raise ValueError("Missing cache_hit boolean")
        if not valid_timing(payload.get("search_latency_ms")):
            raise ValueError("Invalid search_latency_ms")
        engine_ms = payload.get("elasticsearch_latency_ms")
        if engine_ms is not None and not valid_timing(engine_ms):
            raise ValueError("Invalid elasticsearch_latency_ms")
        if uncached and (
            response.headers.get("X-CodeAtlas-Cache-Bypassed") != "true"
            or payload["cache_hit"] is not False
            or engine_ms is None
        ):
            raise ValueError("Server did not confirm cache bypass")
    except ValueError:
        result.error = "Invalid search response"
        return result

    result.cache_hit = payload["cache_hit"]
    result.search_ms = payload["search_latency_ms"]
    result.elasticsearch_ms = engine_ms
    return result


async def check_health(client):
    try:
        response = await client.get("/health")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "healthy":
            raise ValueError("Health response does not report healthy")
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Health check failed ({type(exc).__name__}): {exc}", flush=True)
        return False
    return True


def percentile(values, p):
    # Same floor-index convention as the existing engine benchmark.
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * p)] if ordered else None


def average(values):
    return statistics.mean(values) if values else None


def display(value):
    return "N/A" if value is None else f"{value:.3f}"


def summarize(results, elapsed, workers):
    successful = [result for result in results if result.error is None]
    latencies = [result.latency_ms for result in successful]
    hits = sum(result.cache_hit is True for result in successful)
    return {
        "workers": workers,
        "total": len(results),
        "successful": len(successful),
        "failed": len(results) - len(successful),
        "statuses": Counter(
            result.status_code for result in results if result.status_code is not None
        ),
        "no_response": sum(result.status_code is None for result in results),
        "errors": Counter(result.error for result in results if result.error),
        "throughput": len(successful) / elapsed if elapsed > 0 else 0.0,
        "average": average(latencies),
        "median": statistics.median(latencies) if latencies else None,
        "p50": percentile(latencies, 0.50),
        "p95": percentile(latencies, 0.95),
        "p99": percentile(latencies, 0.99),
        "min": min(latencies) if latencies else None,
        "max": max(latencies) if latencies else None,
        "hits": hits,
        "misses": len(successful) - hits,
        "hit_pct": 100 * hits / len(successful) if successful else None,
        "server_search": average([result.search_ms for result in successful]),
        "server_engine": average([
            result.elasticsearch_ms for result in successful
            if result.elasticsearch_ms is not None
        ]),
    }


def print_report(summary):
    print(f"Total requests      : {summary['total']}")
    print(f"Successful requests : {summary['successful']}")
    print(f"Failed requests     : {summary['failed']}")
    print(f"Status codes        : {dict(sorted(summary['statuses'].items()))}")
    print(f"No HTTP response    : {summary['no_response']}")
    if summary["errors"]:
        print(f"Failure categories  : {dict(summary['errors'])}")
    print(f"Throughput          : {summary['throughput']:.2f} successful req/s")
    print("Client latency (successful requests, ms):")
    for key in ("average", "median", "p50", "p95", "p99", "min", "max"):
        print(f"  {key:<18}: {display(summary[key])}")
    print(f"Cache hits          : {summary['hits']}")
    print(f"Cache misses        : {summary['misses']}")
    print(f"Cache-hit percentage: {display(summary['hit_pct'])}% of successes")
    print("Server-reported latency (successful requests, ms):")
    print(f"  search_latency_ms avg       : {display(summary['server_search'])}")
    print(f"  elasticsearch_latency_ms avg: {display(summary['server_engine'])}")


async def benchmark(workers, *, uncached=False, base_url=BASE_URL):
    print(f"\nCONCURRENCY: {workers}", flush=True)
    async with httpx.AsyncClient(
        base_url=base_url,
        limits=httpx.Limits(
            max_connections=workers, max_keepalive_connections=workers
        ),
        timeout=httpx.Timeout(120.0, connect=5.0, pool=5.0),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        if not await check_health(client):
            print("Skipped this concurrency level; no measured requests sent.")
            return None

        # Warm all query/limit cache keys without flushing Redis.
        # A failed warm-up request does not abort subsequent requests.
        warmup = [await run_request(client, query, uncached) for query in QUERIES]
        warmup_failures = sum(result.error is not None for result in warmup)
        print(
            f"Warm-up: {len(warmup)} excluded requests, "
            f"{warmup_failures} failures",
            flush=True,
        )
        if warmup_failures:
            print("Warm-up incomplete; inspect measured cache hits/misses.", flush=True)
            if uncached:
                print("Skipped: uncached warm-up must succeed and confirm bypass.")
                return None

        # Fixed workers bound in-flight requests without timing queue wait.
        pending = iter(range(TOTAL_REQUESTS))
        results = []

        async def worker():
            for index in pending:
                results.append(await run_request(client, QUERIES[index % len(QUERIES)], uncached))

        start = perf_counter()
        await asyncio.gather(*(worker() for _ in range(workers)))
        elapsed = perf_counter() - start

    summary = summarize(results, elapsed, workers)
    print_report(summary)
    return summary


async def main(uncached=False, base_url=BASE_URL):
    mode = "UNCACHED" if uncached else "WARM-CACHE"
    print(f"CODEATLAS {mode} HTTP SEARCH BENCHMARK")
    print(f"Endpoint: {base_url}/search; limit={SEARCH_LIMIT}")
    print(f"Measured requests per level: {TOTAL_REQUESTS}; Redis is never flushed.")
    print("Client percentiles use successful requests; throughput includes failure time.")
    print("Server elasticsearch_latency_ms measures the whole hybrid engine call.")
    if uncached:
        print("Cache misses below are intentional bypasses; no Redis reads or writes.")
    summaries = []
    for workers in CONCURRENCY_LEVELS:
        summaries.append((workers, await benchmark(workers, uncached=uncached, base_url=base_url)))

    print("\nCOMPARISON (latencies in ms; success-only latency and cache statistics)")
    print(
        f"{'Workers':>7} {'Total':>6} {'OK':>5} {'Fail':>5} {'OK/s':>9} "
        f"{'Avg':>9} {'p50':>9} {'p95':>9} {'p99':>9} "
        f"{'Hit%':>8} {'Srv avg':>9} {'Engine avg':>10}"
    )
    for workers, summary in summaries:
        if summary is None:
            print(f"{workers:>7} SKIPPED (preflight failed)")
            continue
        print(
            f"{workers:>7} {summary['total']:>6} {summary['successful']:>5} "
            f"{summary['failed']:>5} {summary['throughput']:>9.2f} "
            f"{display(summary['average']):>9} {display(summary['p50']):>9} "
            f"{display(summary['p95']):>9} {display(summary['p99']):>9} "
            f"{display(summary['hit_pct']):>8} {display(summary['server_search']):>9} "
            f"{display(summary['server_engine']):>10}"
        )
    return all(summary is not None and summary["failed"] == 0 for _, summary in summaries)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uncached", action="store_true", help="Require intentional server cache bypass")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.uncached, args.base_url)) else 1)
