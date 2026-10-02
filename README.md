# CodeAtlas

CodeAtlas indexes GitHub repositories into searchable code symbols. It combines
lexical BM25 retrieval with semantic vector retrieval and exposes search through
FastAPI. PostgreSQL stores metadata, Elasticsearch stores searchable symbols
and embeddings, and Redis caches results.

The current corpus contains 4,340 Python symbols across micrograd, click,
requests, httpx, itsdangerous and markupsafe. The system includes incremental
Git synchronization, signed GitHub push webhooks, optional cross-encoder
reranking, retrieval evaluation and concurrent benchmarks.

## Local setup

Use Python 3.13 and Docker Compose. The development environment is macOS/arm64.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
docker compose up -d
docker compose ps
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Only copy the environment template for a new setup; preserve existing `.env`
values. Compose runs PostgreSQL 17, Elasticsearch 9.4.3 and Redis 7 on ports
5432, 9200 and 6379. These are local development services, not a secured public
deployment. FastAPI runs on the host. First semantic search/indexing downloads
`sentence-transformers/all-MiniLM-L6-v2`; optional reranking uses
`cross-encoder/ms-marco-MiniLM-L-6-v2`. Once cached, `HF_HUB_OFFLINE=1` avoids
external model lookups.

Interactive API documentation: http://127.0.0.1:8000/docs. Check dependencies
with `GET /health`. `docker compose stop` preserves data volumes.

## Ingest and index

```sh
curl -X POST http://127.0.0.1:8000/repositories \
  -H 'Content-Type: application/json' \
  -d '{"clone_url":"https://github.com/karpathy/micrograd.git"}'
.venv/bin/python -m scripts.batch_ingest
.venv/bin/python -m scripts.index_all_symbols
.venv/bin/python -m scripts.index_all_elasticsearch
```

Ingestion clones GitHub repositories and discovers source files. Symbol indexing
currently parses Python only, using Tree-sitter. Elasticsearch indexing builds
384-dimensional normalized embeddings and searchable symbol documents. The
batch script adds the other five corpus repositories. These operations mutate
the corpus; do not reindex just to run a benchmark.

Full indexing persists recovery jobs and stages complete Elasticsearch generations
before atomically switching a search alias. It copies unaffected repositories
and excludes cooperating writers during publication. After symbol preparation,
finish Elasticsearch publication before syncing that repository. Resume a
pending publication with `scripts.index_elasticsearch --repository-id ID` after
resolving its dependency failure; failed batches exit nonzero. Restart all API
and writer processes together to create the additive job tables and adopt the
new routing/locks. Retired and abandoned indices are retained. See
[atomic publication](docs/atomic-publication.md) for recovery, migration and limits.
Use `scripts.manage_index_generations inspect` for a read-only inventory and
`plan --output PATH` for a reviewed cleanup plan. Applying a plan requires stopped
readers/writers and an explicit `--quiesced` attestation; active, legacy, aliased,
journaled and untracked indices stay protected. See
[generation retention](docs/index-generation-retention.md).

`GET /repositories` lists registered repositories. To update one, POST to
`/repositories/{id}/sync`. Synchronization fetches Git changes, updates metadata
and symbols, replaces affected Elasticsearch documents and invalidates search
cache entries. It resets the generated clone to the fetched commit: do not
edit generated clones. Signed push events to `/webhooks/github` trigger the
same synchronization; set `GITHUB_WEBHOOK_SECRET` before using webhooks.

Sync records unfinished publication work in PostgreSQL and advances its
checkpoint only after Elasticsearch updates and cache invalidation succeed.
Retrying `/repositories/{id}/sync` resumes a pending target; `resumed=true`
identifies that case. Concurrent sync requests for the same repository return
HTTP 409. See [sync recovery](docs/sync-recovery.md) for failure handling,
installation and operational limits. Full indexers reject pending jobs; do not
run full indexing concurrently with sync.

## Search

```sh
curl -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"parse command line arguments","limit":10}'
```

`query` is required; `limit` defaults to 10 and ranges from 1 to 100. The default
path checks Redis, runs BM25 and vector retrieval concurrently on a miss,
normalizes and fuses scores (60% semantic / 40% BM25 by default), then caches
the results. Reranking is available to evaluation code but is not enabled on
the default API. Test code is excluded unless the query indicates test intent.
Search requires an existing symbol index and never creates one. Run the
indexing workflow before searching a new installation.

Cache reads capture a generation token; fills are accepted only while that
generation remains current. Invalidation rotates the token atomically, so an
older in-flight search cannot refill the current cache. Redis failures still
fall back to retrieval. See [cache consistency](docs/cache-consistency.md) for
rollout requirements and the remaining snapshot limitations.

Responses include source code, symbol location, component scores/ranks,
`cache_hit` and timing fields. `search_latency_ms` covers the search service;
the legacy name `elasticsearch_latency_ms` covers the entire hybrid engine,
including embedding and fusion. Neither is client end-to-end latency.

## Embedding execution settings

Device selection is automatic by default. To reproduce the measured CPU
configuration on this Apple Silicon development machine, start the API with:

```sh
EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

These are opt-in process settings, applied when the embedding model first loads;
restart to change them. `TORCH_NUM_THREADS` controls PyTorch intra-op threads
process-wide, including other PyTorch models such as the optional reranker.
Model initialization is serialized across cold requests, while inference remains
concurrent. No query embeddings are cached by this configuration. Keep automatic
selection on other hardware until you measure alternatives. CPU inference was
faster for concurrent short-query HTTP workloads here, but did not reliably
improve single-worker HTTP latency. Batch-indexing performance was not measured.

For isolated embedding diagnostics (not HTTP or retrieval benchmarks):

```sh
HF_HUB_OFFLINE=1 .venv/bin/python -m scripts.benchmark_embeddings --device cpu --torch-threads 1
HF_HUB_OFFLINE=1 .venv/bin/python -m scripts.benchmark_embeddings --device mps
```

Run each configuration in a fresh process and one load test at a time.
`--max-inflight 1` is an experimental diagnostic option only: serialized
inference showed poor tail latency under the thread-pool workload and is not
implemented in the application.

## Tests and retrieval quality

```sh
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -m scripts.evaluate_multirepo
```

The unit suite mocks external services and does not download models. Existing
`scripts/test_*` are manual integration utilities, some with import-time side
effects. Multi-repository evaluation requires the indexed corpus and validates
25 ground-truth cases against PostgreSQL. Track invalid cases as well as
metrics. The inherited hybrid Recall@10 baseline is **0.880**; preserve quality
when changing retrieval, ranking or indexing.

## Reproducible performance measurements

```sh
# Direct engine: Redis and HTTP bypassed.
.venv/bin/python -m scripts.benchmark_concurrent
# Warm-cache HTTP: default mode, never flushes Redis.
.venv/bin/python -m scripts.benchmark_api
# Component diagnostics (not HTTP benchmarks).
.venv/bin/python -m scripts.profile_search_latency
.venv/bin/python -m scripts.profile_concurrent
```

For controlled uncached HTTP, start a dedicated development server in another
terminal. Port 8001 isolates it from the normal API:

```sh
BENCHMARK_CACHE_BYPASS_ENABLED=true APP_ENV=development \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 \
  --workers 1 --no-access-log --no-proxy-headers
.venv/bin/python -m scripts.benchmark_api --uncached \
  --base-url http://127.0.0.1:8001
```

The bypass is disabled by default. It requires development/test mode, a
loopback peer, and `X-CodeAtlas-Benchmark-Bypass: true`. It skips Redis reads
and writes while using the normal hybrid engine, route and serialization.
The client requires `X-CodeAtlas-Cache-Bypassed: true`, `cache_hit=false` and an
engine timing. Rejected or unacknowledged bypass is a failure, not a valid
uncached measurement. Bind this server only to loopback, disable proxy headers,
and do not expose it through a reverse proxy. Stop it after benchmarking.

All concurrent benchmarks preserve the same ten queries, limit 10, 200 measured
requests at concurrency 1/5/10/20. HTTP runs use pooled connections and fixed
workers, check health before each level, and exclude ten warm-up requests.
Uncached warm-up still warms models/connections, not Redis. Failures are counted;
client latency statistics use successful responses, while throughput includes
the elapsed time spent on failures. Percentiles use the existing floor-index
convention. Nonzero exit status indicates failed requests or skipped levels.

Client timings cover request send through complete response-body receipt and
exclude client queue wait and JSON validation. Report server timings separately.
`profile_search_latency` executes stages sequentially; the real engine executes
retrieval branches concurrently. `profile_concurrent` instruments overlapping
stages and adds overhead: its stage averages must not be summed.

To investigate the resolution delay with read-only request-sequencing controls:

```sh
EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 HF_HUB_OFFLINE=1 \
  .venv/bin/python -B -m scripts.profile_resolution
```

Use an already cached model and avoid concurrent indexing/publication. This
diagnostic times lookups after embedding or retrieval, and compares other
Elasticsearch endpoints and a separate connection pool. It bypasses Redis by
calling the engine directly; these measurements are not HTTP or capacity results.

`scripts.profile_search_transport` adds response-size, gzip and connection-close
controls without loading models. Source-free requests are diagnostic controls;
production search always returns the same result fields.

An optional local transport workaround, `ELASTICSEARCH_CLOSE_SEARCH_CONNECTIONS=true`,
closes Elasticsearch connections after BM25/vector search responses. It preserves
generation pinning and leaves alias/writer connection reuse enabled. On this Mac
with local Docker it reduced single-worker uncached latency, but reduced throughput
at higher concurrency. It defaults to false; restart the API to change it and
benchmark your workload before choosing it. `.env` is unchanged. See the
[measurements and tradeoff](docs/performance.md).

Keep **warm-cache HTTP**, **uncached HTTP**, **direct engine**, and **component
profiling** results separate. Short runs indicate a throughput knee, not a
proven sustained capacity ceiling. See [measured results](docs/performance.md)
for baselines, the index-probe experiment, and quality verification;
[AGENTS.md](AGENTS.md) holds persistent development context and inherited baselines.

## Development map and limitations

`app/api` contains routes; `app/core` settings/clients; `app/db` metadata models;
`app/indexer` ingestion/parsing/sync; `app/search` retrieval/cache/reranking;
`scripts` operational tools; `tests` offline regression tests.

Full repository rebuilds publish an Elasticsearch generation atomically and
exclude stale target documents. PostgreSQL metadata, Redis cache visibility and
Elasticsearch publication remain separate commits; a batch is not one snapshot.
Incremental sync updates the current index in place and can expose intermediate
states while its recovery job is pending. Generation cleanup requires maintenance
because readers have no leases. Simultaneous cache misses are not coalesced.
Public deployment hardening and the underlying transport cause of the measured
resolution delay remain unresolved; an opt-in connection workaround is available.
