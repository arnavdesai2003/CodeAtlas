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

## Embedding execution experiment (completed 2026-09-28)

Investigated the remaining embedding bottleneck using the same model, corpus,
ten queries and HTTP methodology. No ranking, candidate count, indexing, Redis
or Elasticsearch changes. Added optional `EMBEDDING_DEVICE` and
`TORCH_NUM_THREADS` settings; unset preserves automatic device selection and
PyTorch's existing thread setting. The latter affects PyTorch process-wide.
Added a model-initialization lock because `lru_cache` alone can construct
multiple models on concurrent cold requests. The lock does not cover inference.

### Uncached HTTP comparison

Fresh automatic/MPS control → CPU/one-thread → CPU repeat → automatic/MPS
repeat, run sequentially on the isolated loopback API at port 8001. Server
processes were restarted between configurations; one Uvicorn worker, no reload,
access logging or proxy headers. Each run: 800 successful HTTP 200 responses,
zero failures, zero cache hits, bypass confirmed. Warm-up excluded.

Throughput in successful requests/second:

| Workers | Auto/MPS control | CPU, 1 thread | CPU repeat | Auto/MPS repeat |
|---|---:|---:|---:|---:|
| 1 | 47.30 | 46.39 | 48.71 | 46.00 |
| 5 | 134.10 | 156.93 | 163.18 | 145.93 |
| 10 | 145.35 | 171.88 | 170.79 | 153.78 |
| 20 | 140.32 | 162.60 | 165.22 | 147.11 |

Client-observed p95 latency in milliseconds:

| Workers | Auto/MPS control | CPU, 1 thread | CPU repeat | Auto/MPS repeat |
|---|---:|---:|---:|---:|
| 1 | 33.140 | 32.943 | 32.211 | 32.766 |
| 5 | 55.358 | 41.529 | 38.902 | 45.975 |
| 10 | 89.297 | 69.113 | 74.402 | 80.308 |
| 20 | 236.639 | 145.456 | 142.379 | 165.412 |

The CPU configuration consistently improves concurrent throughput/tails here;
at ten workers it achieves about 11–18% more throughput than the two controls.
Single-worker HTTP performance is effectively unchanged within these runs.
Twenty workers still add waiting rather than throughput; ten remains a useful
operating point for this short workload. No sustained-capacity claim is made.
Automatic selection remains the application default: these results do not
establish CPU as preferable on other machines, for long queries, or for batch
indexing. The existing port 8000 API and `.env` were not changed.

Reproduce the candidate server (client benchmark command remains unchanged):

```sh
EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 \
  BENCHMARK_CACHE_BYPASS_ENABLED=true APP_ENV=development HF_HUB_OFFLINE=1 \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 \
  --workers 1 --no-access-log --no-proxy-headers
.venv/bin/python -m scripts.benchmark_api --uncached --base-url http://127.0.0.1:8001
```

### Embedding-only experiments and rejected limiter

`scripts.benchmark_embeddings` loads a model on the requested device in a fresh
process, warms all ten queries, then measures 200 calls at 1/5/10/20 workers.
It includes optional semaphore wait and excludes executor queue wait. Numerical
comparison occurs after measurement against serial references on the same
device; all observed maximum absolute differences were zero. This is not a
cross-device equivalence proof or an HTTP/retrieval-quality benchmark.

CPU/one-thread initially measured 428.44 req/s at one worker and 258.32 at
twenty; CPU/two-thread measured 394.01 and 259.79. Automatic MPS/four-thread
measured 270.34 and 206.96. Initial CPU runs were sandboxed; MPS required
unsandboxed accelerator access. Use the HTTP controls above for the matched
environment comparison rather than interpreting these as definitive device
speed ratios.

An unsandboxed CPU/one-thread verification then measured 438.17/351.01/258.77/
260.43 embedding calls/sec at 1/5/10/20 workers, with p95 2.499/16.224/43.973/
85.993 ms and zero numerical differences from serial references. This confirms
the CPU diagnostic trend without relying solely on sandboxed measurements.

Serializing inference with a semaphore improved aggregate embedding-only
throughput but caused severe tail waiting: at twenty workers CPU/one-thread
measured 433.49 req/s with p95 437.042 ms, and MPS measured 289.05 req/s with
p95 654.801 ms. Short calls repeatedly reacquire the semaphore while other
threads wait. The limiter remains diagnostic-only and was **not** added to
application inference. HTTP would interleave other work, but these tail results
do not justify imposing a simple semaphore on production search.

### Quality and checks

`EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 HF_HUB_OFFLINE=1 python -m
scripts.evaluate_multirepo` completed with 25 valid / 0 invalid cases. Every
metric for BM25, semantic, hybrid and reranked retrieval matched the previously
recorded table exactly: hybrid Recall@10 .880, MRR .499. Existing index vectors
were reused; no reindexing. The unit suite now has 28 passing offline tests,
including default configuration preservation, explicit device/thread setup,
normalized encoding, validation of thread count and single model initialization
under simultaneous cold calls.

Next priorities: expand ingestion/incremental-sync recovery tests, especially
failure between PostgreSQL commit and Elasticsearch update. Avoid further
ranking changes to chase the remaining hardware/transport latency. Longer
duration performance trials and more representative queries are needed before
production concurrency recommendations.

## Cache generation fencing (2026-09-28)

Correctness change: reject in-flight stale cache fills after invalidation. See
[cache consistency](cache-consistency.md). Same Apple Silicon host, Python 3.13,
one Uvicorn worker on loopback port 8001, no access log/proxy headers, automatic
embedding settings and offline model loading. Before server used the previous
commit; after server loaded the generation implementation. Each run used the
existing ten queries, limit 10, 200 requests per level, excluded warm-up and
pooled HTTP client. Redis was not flushed. Port 8000 was not restarted.

### Warm-cache HTTP only

All 800 requests in each of three runs succeeded with 100% cache hits.

| Workers | Before req/s | After req/s | After repeat req/s | Before p95 ms | After p95 ms | Repeat p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 67.23 | 66.62 | 66.40 | 15.986 | 16.298 | 16.292 |
| 5 | 355.39 | 343.87 | 359.17 | 17.711 | 20.953 | 19.422 |
| 10 | 675.63 | 671.40 | 757.20 | 27.436 | 20.768 | 19.444 |
| 20 | 1114.30 | 895.34 | 984.56 | 31.396 | 46.797 | 26.922 |

Warm hits still make one Redis round trip, now running an atomic Lua read
instead of GET. At twenty workers both after runs have lower throughput than
the single before run (about 12–20%); tail latency varies substantially. This
is a possible cost, not evidence of a sustained regression or an optimization.
The correctness fix is retained. Longer alternating trials are needed to
attribute the difference to Lua rather than scheduling/host variability.

### Uncached HTTP verification

All 800 requests succeeded, zero cache hits, acknowledged bypass. Client latency
is distinct from server search time. No retrieval/indexing changes or new
quality claims; the previous full evaluation remains the quality baseline.

| Workers | Successful req/s | Client avg ms | Client p95 ms | Client p99 ms | Server search avg ms |
|---|---:|---:|---:|---:|---:|
| 1 | 48.05 | 20.713 | 32.922 | 34.330 | 19.248 |
| 5 | 141.47 | 34.966 | 49.025 | 52.369 | 32.493 |
| 10 | 143.57 | 69.067 | 90.224 | 96.016 | 65.633 |
| 20 | 142.89 | 138.095 | 230.551 | 244.519 | 132.308 |

Throughput flattens after five workers in this short run; it does not establish
a new capacity ceiling. Cached results above must not be compared as uncached
retrieval throughput. The dedicated benchmark server was stopped afterward.


## Atomic publication routing (2026-10-01)

New measurements for the staged-publication milestone; these are **uncached
HTTP**, not cached throughput, a direct-engine benchmark or a capacity ceiling.
Same Apple Silicon development host, Python 3.13.15, existing Docker services,
one Uvicorn worker on loopback 8001, access logging/proxy headers disabled,
`APP_ENV=development BENCHMARK_CACHE_BYPASS_ENABLED=true`, opt-in
`EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 HF_HUB_OFFLINE=1`. Defaults and `.env`
were not changed. Ten unchanged queries, limit 10, 200 measured requests at
1/5/10/20 workers, ten excluded warm-ups and bypass preflight per level.
HTTP runs were sequential, with no evaluation or smoke load alongside them.

Order: unchanged checkpoint `8669fa4` before → new routing after A → new
routing after B → unchanged checkpoint bracketing control. The final control
loaded the checkpoint's archived `app/` sources from a temporary directory
using the same interpreter, existing `.env` and server settings. It did not
change the working tree or copy secrets. All four runs had 800 successful
HTTP 200 requests, zero failures and zero cache hits (3,200 measured successes).
No Redis flush occurred. The live corpus was not rebuilt or migrated for these
runs: new routing resolved the missing active alias to `codeatlas_symbols`.
A read-only count confirmed 4,340 documents; this alone is not a verification
of each repository's document count.

| Run | Workers | req/s | HTTP avg ms | HTTP p95 ms | HTTP p99 ms |
|---|---:|---:|---:|---:|---:|
| Before | 1 | 51.79 | 19.228 | 31.132 | 32.328 |
| Before | 5 | 147.26 | 33.720 | 42.556 | 47.047 |
| Before | 10 | 162.85 | 60.407 | 79.773 | 91.254 |
| Before | 20 | 149.24 | 132.596 | 197.899 | 224.109 |
| After A | 1 | 26.97 | 36.976 | 46.675 | 53.562 |
| After A | 5 | 124.40 | 39.574 | 54.176 | 58.743 |
| After A | 10 | 153.04 | 64.367 | 83.411 | 90.213 |
| After A | 20 | 148.75 | 131.404 | 199.652 | 212.304 |
| After B | 1 | 26.23 | 38.018 | 50.271 | 53.057 |
| After B | 5 | 137.20 | 36.159 | 51.758 | 54.603 |
| After B | 10 | 150.68 | 65.215 | 90.387 | 92.260 |
| After B | 20 | 156.78 | 125.333 | 148.155 | 157.559 |
| Control | 1 | 52.87 | 18.828 | 30.324 | 34.048 |
| Control | 5 | 163.01 | 30.510 | 37.313 | 46.388 |
| Control | 10 | 171.65 | 57.681 | 71.524 | 76.478 |
| Control | 20 | 158.73 | 122.778 | 200.205 | 218.758 |

This is a measured latency regression, especially at one worker: after-run
average latency was roughly twice the bracketing controls. Ten-worker throughput
was also lower in both after runs. Twenty-worker results varied, so they do not
support a performance improvement claim. The added name resolution binds both
parallel branches to one concrete generation across alias switches; the
correctness requirement is the reason for accepting this recorded tradeoff,
not a latency optimization. Warm cache hits do not call the engine; no new
warm-cache performance measurement is claimed here.

### Diagnostic measurements (separate from HTTP)

A temporary read-only diagnostic timed 100 sequential alias resolutions after
five warm-ups: average 0.426 ms, p50 0.407 ms, p95 0.607 ms. Those tight-loop
numbers did not explain the HTTP regression. A subsequent alternating component
diagnostic used the same CPU/one-thread configuration, ten unchanged queries,
ten excluded warm-ups and twenty timed hybrid calls per block. It alternated
normal resolution and a fixed concrete target, with unchanged retrieval and
ranking. Fixing the target was an experimental control on the unchanged corpus;
it is **not** an application optimization or a safe route during publication.

| Diagnostic block | Alias avg ms | Embedding avg ms | BM25 avg ms | Semantic branch avg ms | Hybrid avg ms |
|---|---:|---:|---:|---:|---:|
| Resolved A | 15.480 | 6.967 | 4.252 | 21.164 | 37.485 |
| Fixed target A | — | 4.320 | 15.948 | 16.984 | 18.398 |
| Resolved B | 15.113 | 6.590 | 4.391 | 22.777 | 38.675 |
| Fixed target B | — | 3.961 | 15.798 | 15.439 | 17.481 |

Stages overlap and are not additive. These short instrumented, sequential
engine calls are not HTTP throughput or the existing production-concurrency
profiler. The interleaved resolution timings, unlike the isolated metadata
loop, show the additional serial dependency in the observed regression. They
do not establish the underlying transport/scheduling cause. An equivalent
`_resolve/index` diagnostic also took roughly 15 ms in that interleaved workload
and did not offer a demonstrated fix; application routing continues to use the
alias API. Ranking, candidate sizes and query text were not adjusted.

The component profiler now pins one concrete index for both sequential stages;
its total includes resolution, while its embedding/BM25/vector component
measurements exclude resolution. It remains distinct from concurrent production
end-to-end latency. No new component-profiler result is claimed here.

All 25 live evaluation cases were valid. Every BM25, semantic, hybrid and
reranked metric reproduced the baseline, including hybrid Recall@10 0.880 and
MRR 0.499. Evaluation used the existing index; it was not a quality evaluation
of a newly published real corpus. Isolated publication checks used deterministic
test vectors, verified copied vectors remained searchable, and removed only
temporary test indices/aliases/Redis keys. See
[atomic publication](atomic-publication.md) for guarantees and recovery.
The dedicated port 8001 servers were stopped; port 8000 was not restarted.


### Real-corpus scratch publication quality

A separate, non-benchmark experiment copied the existing corpus into isolated
indices, used PostgreSQL read-only to populate temporary SQLite metadata for
micrograd, and ran the actual publication workflow with real CPU/one-thread
MiniLM embeddings. It replaced 40 micrograd symbols and copied the other 4,300
documents. This checked quality on a newly published **scratch** generation,
without switching the live alias, changing live repository rows or rotating the
live cache. Cache invalidation was stubbed only for this separate experiment;
the preceding isolated Redis smoke tests exercised the real invalidation path.
Every scratch index and alias was removed afterward.

| Metric | Live BM25 | Scratch BM25 | Live semantic | Scratch semantic | Live hybrid | Scratch hybrid | Live reranked | Scratch reranked |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Recall@1 | .080 | .080 | .440 | .440 | .360 | .360 | .400 | .400 |
| Recall@3 | .240 | .240 | .720 | .720 | .560 | .560 | .520 | .520 |
| Recall@5 | .280 | .280 | .760 | .760 | .760 | .760 | .600 | .600 |
| Recall@10 | .280 | .280 | .880 | .840 | .880 | .880 | .800 | .800 |
| MRR | .168 | .168 | .585 | .581 | .499 | .499 | .493 | .493 |

All 25 cases remained valid. Hybrid passed the Recall@10 .880 gate and every
hybrid/BM25/reranked metric matched. Semantic lost one Recall@10 case relative
to the live corpus; this difference was also observed in two earlier scratch
publication attempts. On the final run, evaluating the **initial scratch source
copy before publication** returned semantic .440/.680/.720/.800 recall at
1/3/5/10 and MRR .561, while its BM25/hybrid/reranked metrics matched. Thus the
observed standalone semantic sensitivity was present in a freshly copied vector
index before the atomic switch, as well as in the published stage. The live and
newly created effective vector mappings matched exactly (`bbq_hnsw`, m=16,
ef_construction=100, oversample=3.0). Staging now explicitly copies effective
source mappings and relevant analysis/similarity/mapping settings to avoid
future defaults changing them.

These observations are consistent with approximate vector graph rebuilding
changing candidate order, but do not identify a unique cause or establish
reproducibility on other hardware or future rebuilds. They do not justify
changing ranking weights, candidate sizes or evaluation cases. Every real
corpus rebuild still requires evaluation; no exact standalone semantic metric
preservation is claimed for the new publication workflow.
