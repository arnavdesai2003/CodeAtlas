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

## Experiment: remove index probes from retrieval

BM25 and semantic retrieval previously called `create_symbol_index()` on every
request, each making an Elasticsearch existence request even when the index
already existed. Indexing already owns creation. Removed only these two calls;
no change to embeddings, corpus, query construction, candidates, weights or
fusion. Search now requires an existing index and never silently creates an
empty one. New installations must index before searching.

Same isolated server configuration, restarted to load the change; same command
and workload as the uncached baseline. All 800 responses succeeded with bypass
acknowledged and zero cache hits.

| Workers | req/s | Avg ms | Median ms | p50 ms | p95 ms | p99 ms | Min ms | Max ms | Server avg ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 50.89 | 19.559 | 16.879 | 16.875 | 31.326 | 32.435 | 14.158 | 38.212 | 18.134 |
| 5 | 140.86 | 35.193 | 33.893 | 33.880 | 47.859 | 49.923 | 16.574 | 52.379 | 32.434 |
| 10 | 144.41 | 68.767 | 68.354 | 68.343 | 84.419 | 92.127 | 22.518 | 93.828 | 65.239 |
| 20 | 143.08 | 137.029 | 137.551 | 137.487 | 162.619 | 186.305 | 72.006 | 188.812 | 132.452 |

Throughput increased 85.9% at one worker, 37.1% at five, 17.8% at ten, and 6.7%
at twenty versus the uncached baseline. This run reaches a plateau around
five to ten workers; twenty offers no additional throughput and higher tails.
These are observed run comparisons, not guarantees across hardware or workloads.

### Quality and correctness

The offline suite passes 23 tests. It verifies cache read/write bypass,
default caching, API validation and bypass guards, cache outage/invalidation,
parser/diff handling, BM25/vector request construction, hybrid fusion, no index
creation on searches, bounded benchmark workers, excluded warm-ups, and
unacknowledged bypass rejection. It does not yet exhaustively test full
ingestion or cross-store synchronization recovery.

Full `scripts.evaluate_multirepo` rerun: **25 valid / 0 invalid** cases.
All metrics exactly match the inherited baseline:

| Metric | BM25 | Semantic | Hybrid | Reranked |
|---|---:|---:|---:|---:|
| Recall@1 | .080 | .440 | .360 | .400 |
| Recall@3 | .240 | .720 | .560 | .520 |
| Recall@5 | .280 | .760 | .760 | .600 |
| Recall@10 | .280 | .880 | .880 | .800 |
| MRR | .168 | .585 | .499 | .493 |

### Remaining bottleneck diagnostic

The post-change concurrent profiler confirms zero index checks at every level.
Average overlapping stage timings (ms):

| Workers | Embedding | BM25 request | Vector request | Semantic branch | Full hybrid |
|---|---:|---:|---:|---:|---:|
| 1 | 9.256 | 16.717 | 7.344 | 16.799 | 19.029 |
| 5 | 27.795 | 13.600 | 6.040 | 33.943 | 35.697 |
| 10 | 49.161 | 13.643 | 10.662 | 59.926 | 63.681 |
| 20 | 102.794 | 17.659 | 13.679 | 116.587 | 122.517 |

At twenty workers the embedding stage is about 84% of full-hybrid average wall
time in this diagnostic. Embedding execution/queuing is the dominant measured
cost under contention; low-concurrency BM25 transport also merits inspection.
Do not infer that index-probe removal caused BM25 request timing changes from
these separate short runs. The next performance experiment should compare
embedding concurrency/device settings with fixed workload and quality checks,
not reduce candidate counts or change weights to chase latency.

### Repeatability check

Repeated the post-change uncached HTTP benchmark after evaluation/profiling
finished, on the same server. Again 800/800 HTTP 200, zero cache hits, every
bypass acknowledged:

| Workers | req/s | Avg ms | p50 ms | p95 ms | p99 ms | Server avg ms |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 50.30 | 19.788 | 16.930 | 30.922 | 37.881 | 18.343 |
| 5 | 136.05 | 36.541 | 35.135 | 48.602 | 53.172 | 34.155 |
| 10 | 147.85 | 66.743 | 64.446 | 82.920 | 93.525 | 63.573 |
| 20 | 151.47 | 128.403 | 129.270 | 174.843 | 180.704 | 122.897 |

The repeat confirms the low-concurrency improvement and diminishing returns
between five and ten workers. Beyond ten, throughput gains range from -0.9%
to +2.4% across the two post-change runs while p95 roughly doubles. For this
workload, 5–10 concurrent requests is the useful operating region; twenty
mostly increases waiting. Do not describe the highest observed 151.47 req/s
as a guaranteed sustained maximum. The isolated benchmark server is stopped
after measurement; the existing port 8000 API is left untouched.
