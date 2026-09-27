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

Twenty offline tests established; README now documents setup. Requirements
normalized from UTF-16/CRLF to UTF-8/LF with all 69 dependency lines preserved.
Next measured experiment: remove index existence probes from read-only search,
then test, benchmark, evaluate and compare before retaining.
