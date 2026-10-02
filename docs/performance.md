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

## Resolution request-sequencing investigation (2026-10-01)

New read-only diagnostics narrow the previously recorded alias-resolution
regression to request sequencing after Elasticsearch retrieval. They do not
identify the underlying server/transport cause or demonstrate an optimization.
Production code, ranking, queries, candidate sizes and per-request concrete
index pinning remain unchanged.

The checked-in `scripts.profile_resolution` ran on macOS 26.6 arm64, Python
3.13.15, CPU MiniLM with `TORCH_NUM_THREADS=1`, existing Docker Elasticsearch
9.4.3 and cached offline model weights. Ten hybrid calls warmed the model and
connections. Each block then excluded five warm-ups and measured 50 successful
follow-up requests using the unchanged ten queries. BM25 predecessors retrieved
40 candidates (the normal hybrid candidate limit for limit 10); hybrid
predecessors used limit 10. Only the follow-up request is timed, not its
predecessor. All 500 measured follow-ups succeeded. Exceptions terminate the
script with a nonzero exit instead of reporting an incomplete block as success.

| Preceding operation | Timed request | Average ms | p95 ms |
|---|---|---:|---:|
| None | Alias resolution | .434 | .533 |
| Sleep 20 ms | Alias resolution | 2.176 | 2.695 |
| Embedding | Alias resolution | 1.030 | 1.477 |
| BM25 | Alias resolution | 16.161 | 23.102 |
| Hybrid | Alias resolution | 15.401 | 20.788 |
| None, bracketing control | Alias resolution | .521 | .666 |
| BM25 | Alias resolution, separate client/pool | 13.511 | 14.860 |
| BM25 | Cluster info | 19.417 | 23.285 |
| BM25 | Document count | 14.226 | 20.962 |
| BM25 | Alias resolution, closing control | 18.891 | 23.534 |

Embedding alone did not reproduce the roughly 15 ms penalty. BM25 alone did,
without invoking embedding. Info and count requests also became slow after
BM25, so this is not isolated to alias metadata or the missing-alias 404.
A separate connection pool did not restore tight-loop metadata latency.
Routing, index UUID and document count were unchanged at the start/end:
legacy `codeatlas_symbols`, 4,340 documents. These checks are not proof of
unchanged document contents during a run; comparisons require no concurrent
indexing/publication. No store was written, Redis was not used, and no API
server was started or restarted.

Additional temporary controls, run sequentially on the same machine:

- Alternating shared/separate resolver pools (50 measured hybrid calls per
  block after ten warm-ups) yielded resolution averages 13.984/14.289/16.755/
  14.396 ms. Separate pools did not remove the delay.
- `TOKENIZERS_PARALLELISM=false` yielded resolution averages 15.575/14.166/
  15.944/15.188 ms across the same shared/separate block order.
- Alternating Python thread switch intervals .005/.001/.005/.001 seconds
  yielded resolution averages 15.496/14.831/15.591/16.175 ms. This did not
  demonstrate a scheduling-setting fix.
- Alternating configured localhost/explicit IPv4 clients for all retrieval
  requests yielded hybrid averages 42.797/43.223/36.580/40.532 ms (50 measured
  calls per block after ten warm-ups). IPv4 did not demonstrate an improvement.
- An instrumented 50-call hybrid profile attributed most client request wait
  to socket response reads; resolution averaged about 14.8 ms. Instrumentation
  and overlapping threads limit attribution. It cannot distinguish server
  dispatch, network delivery or host scheduling.

These are sequential component diagnostics, not HTTP latency, throughput,
sustained capacity or retrieval-quality evaluations. Short blocks and order
variation limit comparisons. No production setting was changed based on them.
All 122 existing offline tests passed. Retrieval/ranking/indexing were unchanged,
so no new retrieval evaluation or HTTP improvement is claimed. The next targeted
investigation is server/transport tracing of requests immediately after search,
including response-size and connection behavior, while preserving generation
consistency. Cache-miss coalescing remains separate.

## Search response connection workaround (2026-10-01)

Response-size and connection controls now reproduce the resolution penalty
without loading an embedding model. The new read-only
`scripts.profile_search_transport` captures the current production BM25 query
parameters, retrieves 40 candidates for the same ten queries, and times the
following alias lookup. It compares complete hits for full/gzip/closed-connection
requests, fails on exceptions or changed hits/routing/identity/count, and never
uses Redis. Run without concurrent writers/publication. Source-free and zero-hit
requests are diagnostic controls only, never production retrieval changes.

### Component diagnostic (not HTTP)

macOS 26.6 arm64, Python 3.13.15, existing Docker Elasticsearch 9.4.3; five
excluded warm-ups and 30 measured search/lookup pairs per block. All 210 measured
pairs succeeded. Decoded bytes are the node's response-body length after
HTTP decompression, not packet bytes. `took` is Elasticsearch's reported search
time; client search includes network, decompression/deserialization and client
overhead. Neither includes the subsequent alias lookup.

| Control | Decoded response bytes avg | Search client avg ms | ES took avg ms | Following alias avg ms | Alias p95 ms |
|---|---:|---:|---:|---:|---:|
| Full source | 75,293.2 | 4.645 | 2.433 | 16.294 | 22.866 |
| No source | 2,664.4 | 1.632 | .367 | .617 | 1.068 |
| Exclude embedding | 75,293.2 | 3.522 | 1.467 | 15.471 | 22.715 |
| Zero hits | 161.9 | 1.009 | .000 | .575 | .773 |
| Gzip (response encoding verified) | 75,293.2 | 4.063 | 1.067 | 16.470 | 22.346 |
| Close search connection | 75,293.2 | 2.031 | .567 | 1.150 | 1.539 |
| Full source, closing control | 75,293.2 | 2.701 | .967 | 13.811 | 21.226 |

The source-free response avoided the delay; gzip did not. Excluding embedding
made no byte difference on these existing responses. Closing the **preceding
search** connection avoided the delay while returning exactly the same hits.
A temporary separate control opened a fresh alias client after each search and
still measured 14.766 ms average, versus .958 ms after closing the search
connection. Thus simply reconnecting the resolver does not reproduce the benefit.

This narrows the observed behavior to search-response/connection sequencing on
this transport. There is no packet or server-dispatch trace establishing a
unique TCP, Docker or Elasticsearch cause; none is claimed. The small `took`
values also do not measure subsequent alias server dispatch.

### Opt-in application setting and matched uncached HTTP

`ELASTICSEARCH_CLOSE_SEARCH_CONNECTIONS=true` sets `Connection: close` only on
BM25/vector search requests. Both hybrid branches still use the same resolved
concrete index. Alias lookups and writer requests retain connection reuse;
result fields, ranking, candidate limits and query text are unchanged. Default
is false, preserving existing deployment behavior. Restart the API to change
it. `.env` was not modified. This is a measured local workaround with a
concurrency tradeoff, not a universal transport recommendation.

Matched runs used CPU embeddings, `TORCH_NUM_THREADS=1`, cached offline weights,
one Uvicorn process, loopback port 8001, no access logs and no proxy headers.
Each run used the unchanged guarded-bypass workload: ten queries, limit 10,
200 measured requests at each concurrency 1/5/10/20, excluded warm-ups and
bypass acknowledgement. No other diagnostic/evaluation ran alongside HTTP.
Order was before/default → temporary experimental header control → implemented
opt-in A → implemented opt-in B → after/default closing control. The two opt-in
runs reused the same server; the final control restarted only the temporary
server with the setting false. Existing port 8000 was never restarted.

| Run | Workers | req/s | HTTP avg ms | HTTP p95 ms | HTTP p99 ms |
|---|---:|---:|---:|---:|---:|
| Before/default | 1 | 25.72 | 38.783 | 52.909 | 54.806 |
| Before/default | 5 | 144.30 | 34.250 | 50.700 | 56.867 |
| Before/default | 10 | 154.16 | 63.954 | 81.216 | 88.929 |
| Before/default | 20 | 159.64 | 122.411 | 148.753 | 154.929 |
| Opt-in A | 1 | 119.42 | 8.312 | 9.532 | 11.850 |
| Opt-in A | 5 | 168.97 | 29.459 | 34.045 | 41.989 |
| Opt-in A | 10 | 145.35 | 68.169 | 79.433 | 87.239 |
| Opt-in A | 20 | 133.48 | 147.546 | 216.927 | 256.676 |
| Opt-in B | 1 | 119.18 | 8.328 | 9.466 | 11.232 |
| Opt-in B | 5 | 172.33 | 28.905 | 32.694 | 34.678 |
| Opt-in B | 10 | 143.68 | 68.988 | 80.344 | 88.516 |
| Opt-in B | 20 | 144.26 | 136.596 | 162.363 | 171.464 |
| After/default control | 1 | 24.81 | 40.221 | 52.967 | 55.488 |
| After/default control | 5 | 128.31 | 38.569 | 47.593 | 48.336 |
| After/default control | 10 | 158.25 | 62.193 | 87.012 | 90.698 |
| After/default control | 20 | 148.94 | 131.189 | 204.546 | 213.161 |

All four tabled runs had 800 successes, zero failures and zero cache hits
(3,200 measured successes). The separate experimental header control also had
800 successes/zero hits; its 1/5/10/20 throughput was 121.00/169.98/145.48/134.44
req/s, average latency 8.202/29.178/68.072/146.454 ms and p95
9.183/33.873/78.582/211.860 ms. It is supporting experimental evidence, not an
additional implementation run.

Single-worker average latency improved from 38.8–40.2 ms to 8.3 ms in the two
implemented opt-in runs. Five-worker throughput improved, but ten-worker
throughput was lower in both, and twenty-worker throughput was lower than both
controls. Tail latency at twenty varied materially. Reconnecting search sockets
adds overhead and trades connection reuse for this local low-concurrency
benefit. These short runs do not establish sustained capacity, remote/TLS
behavior, benefits on other hosts, or a general default change. No warm-cache
improvement is claimed; hits do not execute Elasticsearch search.

124 offline tests passed, including identical query/results and concrete
index pinning with the setting off/on, and propagation of transport failures.
Opt-in CPU/one-thread multi-repository evaluation had 25 valid/zero invalid
cases and reproduced every baseline metric, including hybrid Recall@10 .880
and MRR .499. Live legacy routing, UUID and 4,340-document count were unchanged;
no corpus/index/cache was modified, no infrastructure was added, and the
port 8001 server was stopped after measurement. Defaults and port 8000 remain
unchanged. Further work should identify the transport cause or test workload
choices before changing defaults; cache-miss coalescing remains separate.

## Forwarding-path isolation (2026-10-01)

New read-only curl controls reproduce the post-search delay without Python's
Elasticsearch HTTP implementation or model inference. The same container curl
binary was tested on both container loopback and `host.docker.internal:9200`.
The latter traverses the host forwarding route back to the existing Elasticsearch
container. This comparison localizes the observed delay to the forwarded path
under these conditions; it does not establish a unique TCP/kernel/proxy cause.

`scripts.profile_forwarding` captures the current production BM25 query parameters
and pins the initial concrete index for all diagnostic searches. It runs one
curl process per search/alias pair; `--next` reuses the search socket for the
following alias request unless the search requested `Connection: close`.
Docker modes execute the existing container's curl and install/write nothing.
All paths disable curl proxies and use HTTP port 9200. No application server,
embedding inference, Redis or writer is involved.

Each location ran separately: host → container loopback → container host route.
Each used four blocks, reuse A → close A → reuse B → close B, with five excluded
warm-up pairs and 20 measured pairs per block. The same ten queries and 40
candidates were used throughout; full search responses averaged 75,293.2 bytes
in every block/location. All 240 measured pairs (480 transfers) completed with
expected statuses: search 200, alias 404 because the active alias is still absent
and the application resolves retained legacy `codeatlas_symbols`. This expected
404 is not treated as a network failure. Curl errors, unexpected statuses,
invalid timing fields, mismatched connection controls or changed metadata cause
nonzero exit rather than a successful completion report.

Host: macOS 26.6 arm64, Python 3.13.15, curl 8.7.1/libcurl 8.7.1 (Apple build).
Container: curl 7.76.1/libcurl 7.76.1 (aarch64 Red Hat build), existing
Elasticsearch 9.4.3. Comparing host to container alone has a client-version
confound; comparing the two container routes uses the same curl binary.

Curl reports cumulative timing phases. `ready` below is `time_pretransfer`;
response wait is `time_starttransfer - time_pretransfer`. This separates
connection/DNS preparation from time awaiting the first response byte, but
response wait still includes sending the request and transport/server work.
It is not a server-dispatch measurement. Timings exclude Python/Docker/curl
process startup and are not CodeAtlas HTTP or capacity measurements.

| Location | Block | Alias ready avg ms | Alias first byte avg ms | Response wait avg ms | Response wait p95 ms |
|---|---|---:|---:|---:|---:|
| Host loopback forwarding | Reuse A | .013 | 16.184 | 16.170 | 22.664 |
| Host loopback forwarding | Close A | .086 | .852 | .766 | 1.080 |
| Host loopback forwarding | Reuse B | .014 | 19.514 | 19.500 | 23.169 |
| Host loopback forwarding | Close B | .093 | 1.089 | .996 | 1.543 |
| Container loopback | Reuse A | .016 | .164 | .148 | .184 |
| Container loopback | Close A | .045 | .240 | .196 | .264 |
| Container loopback | Reuse B | .017 | .157 | .141 | .168 |
| Container loopback | Close B | .044 | .236 | .191 | .215 |
| Container through host forwarding | Reuse A | .020 | 12.403 | 12.384 | 14.005 |
| Container through host forwarding | Close A | .314 | 31.988 | 31.674 | 207.941 |
| Container through host forwarding | Reuse B | .022 | 12.475 | 12.453 | 14.619 |
| Container through host forwarding | Close B | .341 | 22.013 | 21.672 | 207.239 |

The forwarded reused-socket penalty appears almost entirely after connection
preparation, rather than DNS/connect setup. Container-loopback reused requests
avoid it, and reconnecting offers no benefit there. The container's forwarded
close controls also had large intermittent response waits. This further limits
the previous workaround: it remains opt-in for the measured **host caller** and
must not be generalized to clients using other routes.

Preliminary fixed-query curl pairs (ten measured after five warm-ups) similarly
returned a 97,710-byte search response and alias first-byte averages 13.010 ms
on host reuse, .156 ms on container-loopback reuse, and 12.500 ms through the
host route with container curl. These are preliminary controls, separate from
the tabled ten-query run. Initial sandboxed host curl requests failed to connect
and were discarded. The checked-in diagnostic initially rejected curl's small
DNS-cache lookup/zero connect time on reused sockets, then the old container
curl's unavailable reused local-port field (-1); both compatibility checks were
corrected before the completed runs. Connection reuse still requires
`num_connects=0` for the alias transfer and matching local ports when reported.

A requested unprivileged macOS packet capture failed because BPF access was
denied; noninteractive privileged capture was unavailable because sudo required
a password. No packet trace was collected and no delayed-ACK/Nagle explanation
is established. More precise attribution needs packet or forwarding/server
tracing with appropriate access; changing kernel/Docker settings is not justified
by these observations alone.

All 124 existing offline tests passed. Start/end host metadata checks confirmed
unchanged routing, index UUID and 4,340-document count in each completed run.
No application/default/.env/index/corpus/cache/schema setting changed, no API
was started/restarted and no container configuration changed. Retrieval code was
unchanged, so no new quality evaluation or application HTTP gain is claimed.
The earlier opt-in workaround and measured throughput tradeoff remain unchanged;
cache-miss coalescing remains a separate investigation.

## Process-local simultaneous miss coalescing (2026-10-01)

Normal cache misses now share in-flight work for exact query text, limit and
known Redis generation within one API process. Followers have a one-second
wait budget before independent retrieval; at most 128 active keys are retained.
Unknown-generation requests and guarded benchmark bypass remain independent.
Redis hits do not enter the registry. See [coalescing behavior](cache-coalescing.md)
for bounds, failure handling, generation fencing and API timing attribution.
Ranking, corpus, candidates, weights, model/device defaults and index publication
are unchanged. No infrastructure or distributed locking was added.

### Direct-service miss bursts (not HTTP or capacity)

New `scripts.benchmark_miss_burst` uses an unpredictable isolated Redis namespace,
never flushes live cache, and removes its temporary keys. It warms the same ten
queries through the engine, then runs two rounds of ten bursts. Each burst
releases twenty callers of one unchanged query/limit 10 with a barrier. The
private generation is rotated between completed bursts, outside measured time,
to create real simultaneous misses without changing query text. Engine calls
are instrumented and result/cache payload equality is checked for every burst.
Timing excludes local worker startup/barrier waiting. These 400-request runs
are deliberately different from the standard 200-request HTTP workload.

macOS 26.6 arm64, Python 3.13.15, CPU MiniLM, `TORCH_NUM_THREADS=1`, cached offline
weights, Elasticsearch 9.4.3, Redis 7 and unchanged default search connection
reuse. Before/control loaded archived `f194b69` app sources first on PYTHONPATH;
output recorded the loaded service path. No .env file or secret was copied.
Order: archived before → after A → after B → archived closing control.

| Run | Successful requests | Real engine calls | Shared followers | Redis hits | Avg caller ms | p95 caller ms |
|---|---:|---:|---:|---:|---:|---:|
| Before | 400 | 400 | 0 | 0 | 126.475 | 156.416 |
| After A | 400 | 20 | 380 | 0 | 37.108 | 45.037 |
| After B | 400 | 20 | 380 | 0 | 37.481 | 46.693 |
| Control | 400 | 400 | 0 | 0 | 124.690 | 149.765 |

All 1,600 requests succeeded and every burst had matching results/cache fills.
The implemented runs reduced engine calls by 95% for this same-query burst
workload. Shared followers remain cache misses (`cache_hit=false`) and do not
borrow the leader's engine duration. This does not imply a 95% reduction for
mixed queries, multiple API processes, Redis outages, slow leaders that trigger
fallback, or a sustained capacity improvement. Single eligible misses add a
second Redis read and leader result copying. No ordinary single-miss HTTP
latency improvement is claimed.

A small post-cleanup-reporting smoke run (two callers, ten bursts) had 20
successes/ten engine calls/ten shared followers and confirmed the script only
reports completed success after namespace deletion. All benchmark/race namespaces
were removed. An isolated real Redis interleaving with mocked retrieval held an
old leader/follower, rotated generation, retrieved fresh work separately and
then released old work: new fill accepted, old fill rejected by Lua, fresh
entry retained and registry empty. No live generation was rotated by these
fixtures.

### Warm-cache HTTP and guarded bypass (separate measurements)

Loopback port 8001, one Uvicorn process, no access logs/proxy headers, same
CPU/one-thread settings, ten unchanged queries, limit 10 and 200 measured
requests at concurrency 1/5/10/20. Warm-ups/preflight were excluded. Warm-cache
order: archived before → current after A → current after B → current uncached
verification → archived closing warm control. No other timed diagnostic or
evaluation ran alongside these HTTP measurements.

| Warm run | Workers | req/s | HTTP avg ms | HTTP p95 ms | HTTP p99 ms |
|---|---:|---:|---:|---:|---:|
| Before | 1 | 66.01 | 15.058 | 15.988 | 16.512 |
| Before | 5 | 348.76 | 14.223 | 22.923 | 32.254 |
| Before | 10 | 663.58 | 14.461 | 20.963 | 33.657 |
| Before | 20 | 972.59 | 19.924 | 36.350 | 40.778 |
| After A | 1 | 65.84 | 15.090 | 15.989 | 16.163 |
| After A | 5 | 334.49 | 14.648 | 25.994 | 31.156 |
| After A | 10 | 674.70 | 14.594 | 23.341 | 46.991 |
| After A | 20 | 936.63 | 20.537 | 43.080 | 56.539 |
| After B | 1 | 65.23 | 15.228 | 15.987 | 17.195 |
| After B | 5 | 350.76 | 13.957 | 18.637 | 29.420 |
| After B | 10 | 644.57 | 15.025 | 20.479 | 23.636 |
| After B | 20 | 1041.82 | 18.159 | 32.734 | 39.251 |
| Control | 1 | 65.69 | 15.128 | 16.034 | 16.552 |
| Control | 5 | 338.12 | 14.559 | 19.161 | 29.080 |
| Control | 10 | 672.96 | 14.567 | 25.377 | 66.874 |
| Control | 20 | 781.08 | 23.042 | 56.565 | 68.336 |

All four warm runs had 800 successes, zero failures and 100% Redis hits (3,200
measured successes). Single-worker latency stayed near 15 ms; high-concurrency
throughput/tails varied across controls and after runs. These observations do
not establish a warm-cache performance improvement. Normal warm-up can populate
or refresh the ten benchmark entries; live Redis was not flushed or invalidated.

| Current uncached verification | req/s | HTTP avg ms | HTTP p95 ms | HTTP p99 ms |
|---|---:|---:|---:|---:|
| 1 worker | 25.76 | 38.716 | 52.078 | 55.392 |
| 5 workers | 140.53 | 35.056 | 49.060 | 57.990 |
| 10 workers | 155.46 | 63.475 | 83.006 | 86.008 |
| 20 workers | 146.89 | 133.278 | 204.932 | 226.205 |

All 800 uncached requests succeeded with zero hits. The updated client rejects
`cache_coalesced=true` on bypass, so none were accepted as shared work. This is
bypass verification, not a matched before/after uncached speedup claim. Total
HTTP successes across these five runs: 4,000; zero failures. The temporary
server was stopped; port 8000 was not restarted.

### Quality and limits

138 offline tests pass, including deterministic stale-generation interleavings,
bounded waiting/saturation, retry after leader errors (including TimeoutError),
unknown tokens/Redis-write failure, empty/independent results, query/limit
separation and API/bypass attribution. CPU/one-thread evaluation had all 25
cases valid and reproduced every BM25/semantic/hybrid/reranked metric: hybrid
Recall@10 .880 and MRR .499. Read-only stores confirmed six repositories,
4,340 PostgreSQL symbols and 4,340 Elasticsearch documents on legacy routing.
No corpus/index/schema/model/ranking or .env/default device/transport setting
was changed. Generation Lua protocol and maintenance/quiescence requirements
remain unchanged. Process-local sharing does not address cross-process misses
or atomic metadata/cache/index visibility. The forwarding-delay mechanism still
requires additional tracing; no Docker/kernel settings were changed.

## Instrumented HTTP mixed-miss bursts (2026-10-01)

Added `scripts.benchmark_http_miss_burst` and a diagnostic-only server factory.
Apple Silicon macOS 26.6, Python 3.13.15; every API worker used CPU embeddings,
one PyTorch thread and ordinary pooled search connections. Twenty client requests
were released together per burst, five bursts for each query mix. All requests
used unchanged benchmark queries and limit 10. Models were warm in every worker.
Each burst began with an empty private Redis generation. Later arrivals could
hit entries filled during the burst. Client latency includes transport/body read,
excludes the release-gate wait; startup, warming and invalidation are excluded.

Controls loaded the app archived from `f194b69`, before coalescing; current app
was `fd7e566`. Run order: before 1 worker, after 1, before 2, after 2, repeat
after 2, control 2, repeat after 1, control 1. Every run measured 400 requests;
all 3,200 succeeded. Each row below represents 100 requests per run. Paired
values are first/repeat for after, before/closing control for baseline. Engine
calls are independently counted in Redis and checked against response attribution.

| API workers | Distinct queries per burst | Baseline engine calls | After engine calls | Baseline avg HTTP ms | After avg HTTP ms | Baseline p95 HTTP ms | After p95 HTTP ms |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 75 / 67 | 5 / 5 | 88.723 / 84.154 | 43.540 / 44.136 | 131.857 / 136.301 | 50.718 / 52.387 |
| 1 | 2 | 90 / 90 | 10 / 10 | 119.784 / 122.736 | 42.257 / 40.384 | 162.704 / 165.566 | 60.318 / 53.119 |
| 1 | 5 | 98 / 94 | 25 / 25 | 131.820 / 145.914 | 50.984 / 51.521 | 161.272 / 231.349 | 82.971 / 68.990 |
| 1 | 10 | 100 / 98 | 50 / 50 | 131.036 / 131.829 | 84.801 / 72.001 | 161.791 / 175.306 | 121.037 / 91.084 |
| 2 | 1 | 62 / 66 | 10 / 10 | 63.833 / 66.039 | 41.853 / 41.172 | 118.863 / 117.932 | 58.806 / 53.723 |
| 2 | 2 | 85 / 84 | 19 / 17 | 78.020 / 77.462 | 47.966 / 47.284 | 114.557 / 116.877 | 76.742 / 70.781 |
| 2 | 5 | 96 / 93 | 39 / 45 | 87.252 / 83.765 | 56.950 / 62.912 | 119.880 / 127.769 | 77.678 / 86.130 |
| 2 | 10 | 96 / 98 | 72 / 60 | 82.032 / 92.438 | 80.013 / 71.747 | 113.226 / 124.891 | 116.337 / 97.184 |

One-worker runs retrieved once per distinct query per burst. Two workers could
retrieve the same query independently: one-query bursts used two leaders each.
Ten-query after runs reached 75/65 distinct query-worker pairs across five bursts,
with 72/60 actual calls; Redis hits could eliminate some leaders. Distribution
varied with connection assignment. Shared-response counts for the four mixes
were 83/84/74/48 and 76/89/75/48 at one worker; 62/58/52/23 and 67/75/49/34
at two workers. Remaining successful responses were Redis hits or retrievals.
Same-query payload fingerprints matched within every burst.

These synthetic mixes show diminishing sharing as query diversity and process
count grow. The first two-worker ten-query run had little average improvement
and slightly worse p95. There is no general multi-worker capacity claim and no
demonstrated need for distributed locks. Retain bounded process-local coalescing;
representative traffic and failure requirements are needed before extending it.
Timings include diagnostic middleware and a synchronous Redis counter increment
per engine call in both app versions. This overhead is paid more often by the
baseline; measured latency gains cannot be attributed solely to coalescing.
These runs are not the standard uncached bypass benchmark or sustained capacity.

Every run verified unchanged legacy routing, index UUID and 4,340 documents,
then stopped its temporary server and removed only its random Redis namespace.
Port 8000, live cache generation, corpus, ranking, schema, .env and defaults were
untouched. 142 offline tests pass, including diagnostic namespace isolation,
legacy/current attribution, bad worker/timing/envelopes and exited-server cleanup.
Only diagnostic code changed; the preceding milestone's quality evaluation is
inherited, not a new measurement. No new retrieval quality claim is made.
