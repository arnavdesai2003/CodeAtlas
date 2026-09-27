# Performance measurements

## Baseline established 2026-09-26

Measured in this repository on macOS arm64, Python 3.13.15, cached MiniLM model
on `mps:0`, existing Docker services. Elasticsearch aggregation verified 4,340
documents: click 1,992; httpx 1,241; requests 807; itsdangerous 144; markupsafe
116; micrograd 40. No reindexing or retrieval configuration changes.

HTTP server: one Uvicorn worker on loopback port **8001**, access logging and
proxy headers disabled, no reload, development-only cache bypass enabled.
Port 8000 was left running and was not the measurement target. Tests were run
sequentially, not alongside other load tests. Ten unchanged queries, limit 10,
200 measured requests per level, ten excluded warm-ups per HTTP level. Client
connection limit equals concurrency. These are short closed-loop benchmarks,
not sustained/open-loop capacity tests. Tail estimates have only 200 samples.

### Uncached HTTP (new measurement)

`python -m scripts.benchmark_api --uncached --base-url http://127.0.0.1:8001`

All 800 requests returned HTTP 200 with confirmed bypass; zero failures,
timeouts or cache hits. No Redis reads, writes or flushes on this path.

| Workers | req/s | Avg ms | Median ms | p50 ms | p95 ms | p99 ms | Min ms | Max ms | Server avg ms | Engine avg ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 27.37 | 36.410 | 34.027 | 33.817 | 47.463 | 51.468 | 29.523 | 56.665 | 34.436 | 34.435 |
| 5 | 102.72 | 48.393 | 48.083 | 48.041 | 58.295 | 61.666 | 30.908 | 64.614 | 45.940 | 45.939 |
| 10 | 122.54 | 80.863 | 79.329 | 79.282 | 101.777 | 110.501 | 40.873 | 119.090 | 77.457 | 77.456 |
| 20 | 134.08 | 145.868 | 146.854 | 146.611 | 178.288 | 188.940 | 62.050 | 197.827 | 141.390 | 141.390 |

### Direct engine and warm-cache HTTP (new control runs)

`python -m scripts.benchmark_concurrent` and
`python -m scripts.benchmark_api --base-url http://127.0.0.1:8001`.
All 800 warm-cache responses succeeded, 100% hits. Direct engine completed all
800 calls. These replace no historical records: inherited baselines remain in
AGENTS.md for provenance.

| Workers | Direct req/s | Direct avg ms | Direct p95 ms | Cached HTTP req/s | Cached avg ms | Cached p95 ms | Cached p99 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 29.04 | 34.421 | 44.292 | 67.19 | 14.719 | 16.013 | 16.800 |
| 5 | 106.55 | 46.258 | 60.940 | 370.55 | 13.172 | 23.238 | 28.200 |
| 10 | 130.71 | 75.676 | 105.921 | 715.17 | 13.770 | 22.862 | 27.227 |
| 20 | 140.27 | 140.266 | 184.027 | 998.93 | 17.752 | 37.141 | 42.233 |

### Concurrent component diagnostic

`HF_HUB_OFFLINE=1 python -m scripts.profile_concurrent` wraps actual retrieval
functions and Elasticsearch requests in-process; no ranking changes. Average
wall times below overlap and must not be summed. Instrumentation overhead and
run variation mean this is not the authoritative throughput benchmark.

| Workers | Embedding ms | Index check ms (400 calls) | BM25 request ms | Vector request ms | BM25 branch ms | Semantic branch ms | Full hybrid ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 11.700 | 14.854 | 4.268 | 9.255 | 19.478 | 35.949 | 36.487 |
| 5 | 26.162 | 9.968 | 7.311 | 5.743 | 17.256 | 42.139 | 43.526 |
| 10 | 47.543 | 12.473 | 10.105 | 11.950 | 21.964 | 72.818 | 75.598 |
| 20 | 94.446 | 17.275 | 12.781 | 16.770 | 28.060 | 130.764 | 134.454 |

### Interpretation

The measured knee is around ten concurrent requests: doubling to twenty adds
9.4% uncached HTTP throughput but raises p95 by 75.2%. Direct engine shows a
similar knee, locating the main constraint inside retrieval rather than HTTP.
Client minus service average is roughly 2–4.5 ms in this run (an aggregate
difference, not a separately instrumented network measurement).

Embedding is the largest measured stage at high concurrency, with contention
on the MPS execution path a likely contributor. This does not distinguish model
execution, tokenization, accelerator queuing and Python scheduling precisely.
Two index-existence requests per search also impose measurable overhead; they
are avoidable because indexing already creates the index. The next experiment
removes these probes without changing queries, candidates, weights or fusion.
An exact sustained saturation ceiling and optimal embedding device/thread
configuration require further controlled experiments; no such claims yet.
