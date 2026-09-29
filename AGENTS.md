# CodeAtlas working context

Treat repository code and measured output as the source of truth. Inspect git
status before work; preserve unrelated edits. Continue local, reversible,
testable work autonomously. Never fabricate metrics or change retrieval solely
to improve latency. Do not add infrastructure without an evidenced need.

## Architecture and directories

- `app/main.py`: FastAPI app; startup initializes PostgreSQL tables.
- `app/api/`: synchronous POST `/search`, health, repository CRUD subset and
  sync routes; signed GitHub push webhooks schedule background synchronization.
- `app/db/`: SQLAlchemy repository/file/symbol metadata.
- `app/indexer/`: GitHub clone/discovery, Tree-sitter Python symbols, incremental
  Git synchronization. Other source extensions are discovered but only Python
  symbols are extracted.
- `app/search/`: Elasticsearch BM25 + 384-dimensional MiniLM vectors; parallel
  retrieval, min-max weighted fusion (default semantic 0.60), optional
  cross-encoder reranking; Redis cache. Default API does not rerank.
- `app/core/`: environment settings and service clients.
- `scripts/`: ingestion, indexing, evaluation, profiling and benchmarks.
- `data/repos/`: generated clones, never commit.
- `compose.yaml`: PostgreSQL 17, Elasticsearch 9.4.3, Redis 7; API runs on host.

## Setup and commands

macOS Apple Silicon development uses Python 3.13 in `.venv`. From repo root:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env  # only for a new setup; never overwrite existing secrets
docker compose up -d
docker compose ps
docker compose logs --tail=100
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`docker compose stop` preserves data. Do not use `down -v` without explicit
authorization. Do not print or commit `.env`, tokens, model caches or data.

## Indexing and evaluation

POST `/repositories` with `clone_url` clones a GitHub repository and records
files; it does not finish symbol/vector indexing. `scripts.batch_ingest` adds
five repositories after micrograd. Full workflow (mutates corpus):

```sh
.venv/bin/python -m scripts.batch_ingest
.venv/bin/python -m scripts.index_all_symbols
.venv/bin/python -m scripts.index_all_elasticsearch
.venv/bin/python -m scripts.evaluate_multirepo
```

Single-repository scripts currently hardcode ID 1. Incremental POST
`/repositories/{id}/sync` updates symbols/Elasticsearch and invalidates cache.
Avoid unnecessary full reindexing. Full indexing is not an atomic rebuild;
verify stale documents/cache if rebuilding. Evaluation validates 25 expected
symbols against PostgreSQL, excludes invalid cases, and reports four methods.
`tune_hybrid` still uses the older micrograd-only evaluation cases.

## Benchmark methodology and invariants

Keep cached HTTP, uncached HTTP, direct engine, and component profiling results
separate. POST `/search` takes `query` and `limit` (default 10, range 1–100).
Normal search checks Redis; cache misses invoke hybrid search and cache results.
Keys normalize case/whitespace and include limit; default TTL 300 seconds.
Redis failures fall back to retrieval. Do not alter query text to defeat cache.

```sh
.venv/bin/python -m scripts.benchmark_concurrent
.venv/bin/python -m scripts.benchmark_api
.venv/bin/python -m scripts.profile_search_latency
```

Concurrent benchmarks use the same ten queries, limit 10, 200 measured requests
at each concurrency 1/5/10/20. Warm-up is excluded. HTTP client latency includes
transport and response-body reading, excludes local worker queue wait. Server
`search_latency_ms` excludes HTTP overhead; legacy `elasticsearch_latency_ms`
actually times the whole engine including embedding and fusion. The component
profiler executes stages sequentially, unlike concurrent production retrieval.
It is NOT production end-to-end latency. API benchmark never flushes Redis.

After retrieval/ranking/indexing changes run multi-repository evaluation;
material regression from hybrid Recall@10 0.880 fails an optimization unless
explicitly justified. Use baseline → change → test → benchmark → evaluation →
compare. Record machine/server settings and distinguish inherited results from
new measurements. Short 200-request runs locate a knee, not sustained capacity.

## Inherited validated baselines (user-provided, 2026-09-26)

Corpus: 6 repositories / 4,340 symbols: micrograd 40, click 1,992, requests 807,
httpx 1,241, itsdangerous 144, markupsafe 116. Recheck live stores before claiming
these as newly verified counts.

| Metric | BM25 | Semantic | Hybrid | Reranked |
|---|---:|---:|---:|---:|
| Recall@1 | .080 | .440 | .360 | .400 |
| Recall@3 | .240 | .720 | .560 | .520 |
| Recall@5 | .280 | .760 | .760 | .600 |
| Recall@10 | .280 | .880 | .880 | .800 |
| MRR | .168 | .585 | .499 | .493 |

| Workers | Direct engine req/s | Engine p95 ms | Warm-cache HTTP req/s | HTTP avg ms | HTTP p95 ms | HTTP p99 ms |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 28.70 | 43.99 | 67.64 | 14.600 | 15.842 | 16.302 |
| 5 | 113.51 | 54.04 | 376.95 | 13.037 | 27.366 | 32.430 |
| 10 | 133.96 | 99.89 | 665.85 | 14.653 | 23.779 | 28.721 |
| 20 | 142.08 | 181.84 | 855.24 | 21.527 | 42.459 | 55.003 |

All 800 inherited warm-cache HTTP requests succeeded, 100% cache hits. Never
describe 855 req/s as uncached retrieval. Direct engine shows diminishing
returns around 10 workers. Sequential profiler average/p95 ms: embedding
13.71/18.20, BM25 4.57/9.85, vector 16.64/17.60, fusion .09/.20,
total 35.01/40.55.

## Tests, failure modes, and Git

Existing `scripts/test_*` are mostly manual integrations; some run on import
and contact services. Do not indiscriminately collect them as unit tests.
Build offline tests under `tests/` using mocked services; prioritize parser,
ingestion/sync, cache/invalidation, fusion and API validation/failures.
Do not download models in unit tests. Model loading is lazy; warm it before
benchmarks. Cold model downloads, unavailable Docker, stale API processes,
Redis outages, concurrent cache misses and insufficient client connections can
invalidate performance comparisons. Capture failures, not only successes.

Before substantial work run `git status`. After coherent milestones inspect
diff, run appropriate checks/evaluation/benchmarks, and commit descriptive
checkpoints. Stage explicit paths only. Never commit `.venv`, generated clones,
service data, secrets, model caches or temporary benchmark artifacts. Do not
rewrite remote history. Keep this document and README current.

## Current milestone

Guarded uncached HTTP implemented and measured. See `docs/performance.md` for
new measurements and comparisons: 27.37/102.72/122.54/134.08 uncached req/s at
1/5/10/20, all 800 successful and zero cache hits. Observed knee around ten;
embedding dominates high-concurrency component timing, and two index probes
per query add overhead. Do not claim a proven sustained capacity ceiling.

Bypass requires `BENCHMARK_CACHE_BYPASS_ENABLED=true`, development/test mode,
loopback peer and `X-CodeAtlas-Benchmark-Bypass: true`. It skips Redis reads and
writes and acknowledges with `X-CodeAtlas-Cache-Bypassed: true`. Normal requests
remain cached. Bind benchmark servers to loopback with `--no-proxy-headers`,
never expose them via a proxy. Client fails unacknowledged bypass; uncached
warm-up failures skip measurement and cause nonzero exit. Dedicated port 8001
leaves normal port 8000 untouched:

```sh
BENCHMARK_CACHE_BYPASS_ENABLED=true APP_ENV=development .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --workers 1 --no-access-log --no-proxy-headers
.venv/bin/python -m scripts.benchmark_api --uncached --base-url http://127.0.0.1:8001
.venv/bin/python -m scripts.profile_concurrent
.venv/bin/python -B -m unittest discover -s tests -v
```

Twenty-three offline tests established; README now documents setup. Requirements
normalized from UTF-16/CRLF to UTF-8/LF with all 69 dependency lines preserved.
Removed redundant index existence probes from BM25/semantic read paths after
profiling: indexing creates the index, search requires it and never creates an
empty index. Uncached HTTP now measures 50.89/140.86/144.41/143.08 req/s at
1/5/10/20, 800 successes, zero hits. All 25 evaluation cases valid; all retrieval
metrics reproduced exactly, including hybrid Recall@10 .880 and MRR .499.
Post-change profiling shows embedding 102.794 ms versus full-hybrid 122.517 ms
average at twenty workers on MPS. Remaining performance investigation: embedding
execution/queuing and device/concurrency settings; do not tune ranking to hide
this cost. Expand ingestion/sync recovery tests before changing those paths.
Repeat uncached HTTP run: 50.30/136.05/147.85/151.47 req/s, all 800 successful,
zero hits. Beyond ten workers adds at most 2.4% throughput in these two runs
while p95 roughly doubles. Details and limitations are in `docs/performance.md`.
Temporary benchmark server on 8001 is stopped when work finishes; port 8000
was not restarted. No infrastructure, ranking weights or candidate sizes changed.

## Latest milestone: embedding execution (2026-09-28)

Optional `EMBEDDING_DEVICE` and `TORCH_NUM_THREADS` settings added. Both default
to unset, preserving automatic device selection and existing PyTorch threads.
The thread setting is process-wide, including the optional reranker; restart
to change either setting. Model cold initialization is serialized to prevent
duplicate loading, but inference remains concurrent. No embedding cache added.

Measured opt-in `EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1` on this Apple Silicon
machine using unchanged uncached HTTP workload. Two runs at ten workers:
171.88/170.79 req/s, p95 69.113/74.402 ms; bracketing automatic/MPS controls:
145.35/153.78 req/s, p95 89.297/80.308 ms. All four runs 800 successes, zero
cache hits. No reliable single-worker HTTP gain; twenty workers adds waiting.
Do not generalize this preference to other hardware or batch indexing.
Default deployment and `.env` remain unchanged. Details: `docs/performance.md`.

New `scripts.benchmark_embeddings` measures only embedding inference with
explicit device/thread settings; it is not HTTP or direct-engine performance.
Its optional semaphore experiment showed poor tail fairness and is not used
in application inference. Use matched unsandboxed HTTP runs for comparisons:
the sandbox cannot access MPS even though host PyTorch can.

28 offline tests pass. CPU/one-thread multi-repository evaluation reproduced
all metrics, 25 valid cases, hybrid Recall@10 .880 and MRR .499. No model,
embedding dimensions, candidate sizing, weights, corpus or index changes.
Next engineering priority at that checkpoint was ingestion and incremental-sync
failure/recovery coverage; completed in the milestone below.

## Latest milestone: durable sync recovery (2026-09-28)

Fixed premature sync checkpoint advancement and ingestion post-commit cleanup.
`repository_sync_jobs` is an additive PostgreSQL table created by `init_db()`;
it was created in the local development database during verification. It stores
target commit, previous commit, affected paths, file IDs and change counts with
the metadata transaction. Elasticsearch deletion/indexing and strict Redis
invalidation must succeed before checkpoint advancement and job deletion commit.
Retry pending work before fetching new Git changes; `resumed=true` in the sync
response identifies recovery. A newer remote target needs a subsequent sync.

PostgreSQL session advisory locks serialize cooperating sync callers across
metadata commits; contention returns HTTP 409. Full indexers reject existing
pending jobs but do not share this lock: never run full indexing concurrently
with sync. Do not delete pending jobs manually. Recovery is not cross-store
atomicity. Stale cache refills are addressed by the subsequent milestone below.
Older-version failures without a job are not automatically reconstructed.

Ingestion reserves clone directories atomically, builds its response before
commit and preserves clones after ambiguous commit failures. Inspect metadata
before reconciling an orphan clone; never auto-delete possibly committed data.
See `docs/sync-recovery.md` for workflows, validation and limitations.

53 offline tests pass. Transaction/failure tests use temporary SQLite with foreign keys and
mocked external services. Live PostgreSQL verified advisory exclusion across
commit and release on exceptions. Retrieval evaluation reproduced all metrics,
25 valid cases, hybrid Recall@10 .880 / MRR .499; live corpus not reindexed.
Next priority at this checkpoint was in-flight stale cache invalidation.
No new performance claims for the recovery milestone.

## Latest milestone: cache generation fencing (2026-09-28)

Redis Lua reads bind results/misses to a random generation; atomic conditional
writes reject fills after invalidation. Invalidation rotates the generation
before best-effort batched cleanup. Strict mode raises on rotation failure;
cleanup failure does not prevent sync finalization. The sync response count
reports physical deletions, not logical invalidations. Unknown read generation
disables caching for that request; normal retrieval and benchmark bypass remain
unchanged. Tokens have no TTL; random replacements prevent reuse after eviction.

Upgrade all API/indexing/sync processes together: legacy cache clients do not
participate in this protocol. Old entries expire naturally; do not flush Redis.
An already-running caller can receive its old result; index publication is still
not an atomic snapshot. See `docs/cache-consistency.md` and `docs/performance.md`.
61 offline tests pass, including deterministic stale-fill interleavings, Redis
outages, metadata eviction, corrupt entries and cleanup failures. Real Redis
verified Lua behavior and TTL using isolated temporary keys. No ranking/index
changes; the previous retrieval quality baseline remains applicable.
Warm-cache before/after comparisons and uncached bypass verification are in
`docs/performance.md`: all requests succeeded; warm runs had 100% hits and
uncached had zero. Twenty-worker warm throughput was lower in both after runs;
do not claim a performance improvement. Dedicated port 8001 server stopped
after verification; existing port 8000 was not restarted.
Next priorities: coordinated full-index recovery and atomic publication;
simultaneous cache-miss coalescing remains a separate performance investigation.
