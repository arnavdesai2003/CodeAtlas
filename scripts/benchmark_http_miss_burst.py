"""Instrumented HTTP mixed-miss bursts; isolated Redis, loopback server only.

Not the standard 200-request benchmark or sustained capacity. Server startup,
model warming, client gate wait and private invalidation are excluded.
"""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.search import cache, engine
from scripts.benchmark_api import QUERIES, percentile, valid_timing
from scripts.http_auth import benchmark_headers


def validate_response(response, query, ready):
    if response.status_code != 200:
        raise RuntimeError(f"Search HTTP status {response.status_code}")
    pid = response.headers.get("X-CodeAtlas-Diagnostic-Worker")
    if pid not in ready:
        raise RuntimeError("Unknown or restarted diagnostic worker.")
    payload = response.json()
    hit, shared = payload.get("cache_hit"), payload.get("cache_coalesced", False)
    engine_ms = payload.get("elasticsearch_latency_ms")
    if not isinstance(hit, bool) or not isinstance(shared, bool) or hit and shared:
        raise RuntimeError("Invalid cache/coalescing attribution.")
    if not valid_timing(payload.get("search_latency_ms")):
        raise RuntimeError("Invalid service timing.")
    if hit or shared:
        if engine_ms is not None:
            raise RuntimeError("Cache/shared result claims engine execution.")
    elif not valid_timing(engine_ms):
        raise RuntimeError("Retrieved response lacks its own engine timing.")
    if not valid_timing(payload.get("coalescing_wait_ms", 0.0)):
        raise RuntimeError("Invalid coalescing wait timing.")
    results = payload.get("results")
    if (payload.get("query") != query or payload.get("limit") != 10 or
            not isinstance(results, list) or not all(isinstance(row, dict) for row in results) or
            payload.get("count") != len(results) or len(results) > 10):
        raise RuntimeError("Invalid result envelope.")
    fingerprint = hashlib.sha256(json.dumps(results, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return pid, "hit" if hit else "coalesced" if shared else "retrieved", fingerprint


async def measure(args, prefix, process, source):
    client = cache.redis_client
    deadline = monotonic() + 120
    while True:
        if process.poll() is not None:
            raise RuntimeError("Diagnostic server exited during startup.")
        ready = {pid: json.loads(value) for pid, value in client.hgetall(prefix + "ready").items()}
        if len(ready) == args.server_workers:
            break
        if len(ready) > args.server_workers or monotonic() >= deadline:
            raise RuntimeError("Diagnostic worker startup failed or restarted; discard run.")
        await asyncio.sleep(.1)
    if any(Path(row["service_file"]).resolve() != source / "app/search/service.py" for row in ready.values()):
        raise RuntimeError("Workers loaded unexpected app sources.")
    print(json.dumps({"diagnostic": "instrumented HTTP miss bursts, not sustained capacity",
        "label": args.label, "platform": platform.platform(), "python": platform.python_version(),
        "server_workers": args.server_workers, "concurrency": args.concurrency,
        "bursts_per_mix": args.bursts, "workers": list(ready.values())}), flush=True)
    aggregate = {}
    observed_pids = set()
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    base_url = f"http://127.0.0.1:{args.port}"
    async with httpx.AsyncClient(base_url=base_url, headers=benchmark_headers(base_url),
                                limits=limits, timeout=60, trust_env=False, follow_redirects=False) as http:
        # Initialize client connections without populating search entries.
        health = await asyncio.gather(*[http.get("/health") for _ in range(args.concurrency)])
        if any(response.status_code != 200 for response in health):
            raise RuntimeError("Diagnostic health warm-up failed.")
        with patch.object(cache, "CACHE_PREFIX", prefix), \
             patch.object(cache, "ENTRY_PREFIX", prefix + "entries:"), \
             patch.object(cache, "GENERATION_KEY", prefix + "generation"):
            for unique in (1, 2, 5, 10):
                counts, latencies, engine_calls, pair_counts = Counter(), [], 0, []
                for burst in range(args.bursts):
                    cache.invalidate_search_cache(strict=True)
                    client.delete(prefix + "engine_calls")
                    release, all_ready = asyncio.Event(), asyncio.Event()
                    waiting = 0

                    async def request(query):
                        nonlocal waiting
                        waiting += 1
                        if waiting == args.concurrency:
                            all_ready.set()
                        await release.wait()
                        start = perf_counter()
                        response = await http.post("/search", json={"query": query, "limit": 10})
                        elapsed = (perf_counter() - start) * 1000
                        pid, outcome, fingerprint = validate_response(response, query, ready)
                        return query, pid, outcome, fingerprint, elapsed

                    tasks = [asyncio.create_task(request(QUERIES[i % unique])) for i in range(args.concurrency)]
                    await all_ready.wait()
                    release.set()
                    rows = await asyncio.gather(*tasks)
                    per_query = {}
                    for query, pid, outcome, fingerprint, elapsed in rows:
                        if query in per_query and per_query[query] != fingerprint:
                            raise RuntimeError("Same-query payloads differ; discard burst.")
                        per_query[query] = fingerprint
                        counts[outcome] += 1
                        latencies.append(elapsed)
                        observed_pids.add(pid)
                    calls = {pid: int(value) for pid, value in client.hgetall(prefix + "engine_calls").items()}
                    retrieved = sum(row[2] == "retrieved" for row in rows)
                    if sum(calls.values()) != retrieved or not set(calls).issubset(ready):
                        raise RuntimeError("Engine counter does not match response attribution.")
                    pairs = len({(row[0], row[1]) for row in rows})
                    engine_calls += retrieved
                    pair_counts.append(pairs)
                    print(json.dumps({"unique_queries": unique, "burst": burst + 1,
                        "successes": len(rows), "engine_calls": retrieved,
                        "query_worker_pairs": pairs, "calls_by_worker": calls}), flush=True)
                summary = {"successes": len(latencies), "engine_calls": engine_calls,
                    "responses": dict(counts), "query_worker_pairs": sum(pair_counts),
                    "avg_ms": sum(latencies) / len(latencies), "p95_ms": percentile(latencies, .95)}
                aggregate[str(unique)] = summary
                print(json.dumps({"unique_queries": unique, **summary}), flush=True)
    if observed_pids != set(ready):
        raise RuntimeError("Not all configured workers served measured requests.")
    return aggregate


def stop_server(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=10)
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-workers", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--bursts", type=int, default=5)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--label", default="current")
    args = parser.parse_args()
    if not 10 <= args.concurrency <= 40 or args.bursts < 1 or not 1024 <= args.port <= 65535 or args.port == 8000:
        parser.error("Use concurrency 10–40, positive bursts, and an unprivileged port other than 8000.")
    source = args.app_dir.resolve()
    if not (source / "app/search/service.py").is_file():
        parser.error("app-dir must contain app/search/service.py")
    # Refuse an occupied port rather than replacing an existing process.
    import socket
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", args.port))
    prefix = f"codeatlas:diagnostic:httpburst:{uuid4().hex}:"
    env = os.environ.copy()
    env.update(APP_ENV="development", CODEATLAS_HTTP_BURST_NAMESPACE=prefix)
    command = [sys.executable, "-B", "-m", "uvicorn", "scripts.http_miss_burst_server:create_app", "--factory",
        "--app-dir", str(source), "--host", "127.0.0.1", "--port", str(args.port),
        "--workers", str(args.server_workers), "--no-access-log", "--no-proxy-headers", "--log-level", "warning"]
    process = None
    target = engine.resolve_search_index()
    uuid = engine.elasticsearch_client.indices.get_settings(index=target)[target]["settings"]["index"]["uuid"]
    count = engine.elasticsearch_client.count(index=target)["count"]
    with TemporaryDirectory(prefix="codeatlas-http-burst-") as directory:
        log_path = Path(directory) / "server.log"
        try:
            with log_path.open("w") as output:
                process = subprocess.Popen(command, env=env, stdout=output, stderr=output, start_new_session=True)
                summary = asyncio.run(measure(args, prefix, process, source))
        except Exception:
            print(log_path.read_text()[-8000:], file=sys.stderr)
            raise
        finally:
            try:
                stop_server(process)
            finally:
                keys = list(cache.redis_client.scan_iter(match=prefix + "*", count=100))
                if keys:
                    cache.redis_client.delete(*keys)
                if list(cache.redis_client.scan_iter(match=prefix + "*", count=100)):
                    raise RuntimeError("Diagnostic namespace cleanup incomplete.")
    if (engine.resolve_search_index() != target or engine.elasticsearch_client.count(index=target)["count"] != count or
            engine.elasticsearch_client.indices.get_settings(index=target)[target]["settings"]["index"]["uuid"] != uuid):
        raise RuntimeError("Index routing/identity/count changed; discard comparison.")
    print(json.dumps({"completed": True, "server_stopped": True, "namespace_removed": True,
        "index": target, "documents": count, "mixes": summary}), flush=True)


if __name__ == "__main__":
    main()
