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
Batch ingestion reports failures and registration/clone conflicts with a nonzero
exit; inspect metadata and clone state before retrying. `--help` does not ingest.

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
An unset/empty secret disables webhook processing with HTTP 503. Missing or
invalid signatures return 401 before JSON parsing; signed malformed payloads
return 400 and do not schedule synchronization. See
[webhook handling](docs/webhooks.md) for response semantics and limits.
Background completion/failures use the webhook logger; failure records include
repository ID and exception type without raw backend messages.

Sync records unfinished publication work in PostgreSQL and advances its
checkpoint only after Elasticsearch updates and cache invalidation succeed.
Retrying `/repositories/{id}/sync` resumes a pending target; `resumed=true`
identifies that case. Concurrent sync requests for the same repository return
HTTP 409. See [sync recovery](docs/sync-recovery.md) for failure handling,
installation and operational limits, and [repository errors](docs/repository-errors.md)
for mutation error statuses and safe retry guidance. Full indexers reject pending jobs; do not
run full indexing concurrently with sync.
Incremental publication verifies every prepared bulk action succeeded before
cache invalidation and checkpoint finalization; incomplete returns retain replayable work.
Repository URLs must be plain GitHub owner/repository URLs; credentials, ports,
query/fragment suffixes and escaped/ambiguous paths are rejected. The same
[repository guide](docs/repository-errors.md) documents the accepted profile.
Discovery and symbol indexing exclude source symlinks; sync removes stale symbols
when affected regular files become links. See [source selection](docs/source-paths.md)
for recovery behavior and filesystem limits.
Type changes are covered by real local Git recovery tests, including failed
publication and restoration from a symlink to a regular source file.
Python parsing honors encoding declarations and UTF-8 BOMs; invalid encodings
fail with transaction rollback instead of indexing replacement characters.
Concurrent symbol extraction uses an independent Tree-sitter parser per call.
Clone-root, owner and clone directory components also reject symlinks/non-directories
before ingestion, new Git sync or source reads; rejected paths require inspection.
Git commands disable terminal input and use a configurable 120-second per-command
timeout. See [Git execution](docs/git-execution.md) for settings and recovery limits.
Child Git commands discard inherited repository-location/object/index overrides;
credential helpers and Git configuration remain operator-controlled.
New Git sync requires a clone-owned `.git` directory and uses explicit metadata/
worktree paths; redirected or missing metadata requires inspection.
Incremental diffs use NUL-delimited paths, preserving tab/newline filenames and
rejecting incomplete records before reset or metadata changes.
HTTP bodies have a configurable 1 MiB limit; oversized requests return 413 before
route work. See [request limits](docs/request-limits.md), especially for large webhooks.
Body receipt also has a configurable 30-second total deadline; stalled uploads
return 408 without starting route work. Search/sync execution remains separate.
Run `.venv/bin/python -B -m scripts.verify_http_receipt` for isolated real-socket
checks of stalled, oversized and exact-limit fixed/chunked bodies.
HTTP/1 body-limit/deadline rejections close the connection; accepted requests
keep normal connection reuse.

## Search

Numeric settings validate at startup: semantic weight must be finite in [0, 1]
and cache TTL must be positive. See [settings validation](docs/settings-validation.md).
Settings string/repr redact keys and omit service URLs; raw configuration mappings
still require careful handling.

Outside development/test, configure `API_KEY` and send `X-CodeAtlas-API-Key` on
search/repository requests. A configured key is enforced in local modes too;
health and signed webhooks remain separate. See [API access](docs/api-access.md).
For keyed local HTTP benchmarks, set `CODEATLAS_BENCHMARK_API_KEY` to match the
server key; benchmark credentials are sent only to loopback targets.

Queries must contain non-whitespace text and be at most 4,096 characters. Invalid
queries return 422 before retrieval; valid query text is preserved exactly.

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

The offline suite passes **413 tests** (2026-10-05), mocks external services and
does not download models. Existing `scripts/test_*` are manual integration
utilities, some with import-time side effects; collect only `tests/`.

Recovery tests use shared file-backed SQLite and bounded spawned-process fixtures.
They cover sync checkpoints, publication stages, ingestion clone preservation,
directory reservation races and reviewed retention. Abrupt child exits bypass
ordinary cleanup; external side effects remain simulated. See
[hardening validation](docs/hardening-validation.md),
[sync/ingestion recovery](docs/sync-recovery.md),
[atomic publication](docs/atomic-publication.md) and
[generation retention](docs/index-generation-retention.md) for boundaries and limits.

Separate live probes verified [Redis cache protocols](docs/cache-consistency.md#isolated-redis-protocol-verification-2026-10-04),
[scratch Elasticsearch/Redis publication](docs/atomic-publication.md#isolated-publication-protocol-verification-2026-10-04)
and [PostgreSQL writer coordination](docs/writer-lock-verification.md), including
release after owner termination. These checks do not establish cross-store
atomicity, database/server crash recovery, retrieval quality or capacity.
The four protocol/lock verification commands handle `--help` without running
probes and reject unsupported arguments before scratch/service operations.

HTTP boundary checks also cover literal boolean health acknowledgements and
signed webhook rejection of nonstandard JSON constants before lookup/scheduling.
Repository sync IDs are bounded to positive int32 values before service/lock work;
the read-only PostgreSQL coordination probe passed again after this validation.
Both `scripts.index_symbols` and `scripts.index_elasticsearch` accept
`--repository-id ID` (default 1), validating before session creation. See
[single-repository commands](docs/full-index-recovery.md#single-repository-command-selection-2026-10-04).
Symbol commands distinguish prepared/retained snapshots from pending publication
and print the next Elasticsearch command with the selected repository ID.
Use `scripts.inspect_recovery_jobs` to inspect pending metadata journals without
Elasticsearch/Redis access; [recovery inspection](docs/recovery-inspection.md)
explains conservative resume hints and snapshot limits.

Multi-repository evaluation requires the indexed corpus and validates
25 ground-truth cases against PostgreSQL. Track invalid cases as well as
metrics. The inherited hybrid Recall@10 baseline is **0.880**; preserve quality
when changing retrieval, ranking or indexing.
Evaluation exits nonzero if any ground-truth case is invalid, while still reporting
valid-subset metrics; an empty valid set skips retrieval. `--help` does not evaluate.
CLI database/model/backend failures exit 1 with sanitized output marking any
partial results incomplete; they do not trigger automatic evaluation retries.
Repository names must resolve uniquely for ground-truth validation; ambiguous
names are invalid cases rather than arbitrarily selecting a GitHub owner.
The [2026-10-04 live checkpoint](docs/validation-checkpoint-2026-10-04.md)
reproduced every metric with 25 valid cases and verified six repositories,
4,340 symbols/documents, legacy routing and no pending jobs.

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

`scripts.profile_forwarding` compares paired curl search/alias requests on the
host, container loopback and the container's route through the host forwarding
port. Run each separately without other load; Docker modes use the existing
Elasticsearch container and install nothing:

```sh
.venv/bin/python -B -m scripts.profile_forwarding --location host
.venv/bin/python -B -m scripts.profile_forwarding --location container
.venv/bin/python -B -m scripts.profile_forwarding --location container-host-route
```

It separates connection setup from response wait, checks connection reuse and
records curl versions. Timings exclude Docker/curl process startup and are not
application HTTP benchmarks. It requires the local unauthenticated HTTP setup
on port 9200. The same container curl showed the delay only on the forwarded
route; a specific TCP mechanism remains unproven.

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
because readers have no leases. Simultaneous misses with exact query/limit and
known generation share work within each API process, with bounded waits and
independent fallback. See [cache coalescing](docs/cache-coalescing.md) for scope,
failure behavior and the new `cache_coalesced`/`coalescing_wait_ms` fields.
Cache values exceeding the requested row limit or containing non-finite JSON
numbers fall back to retrieval; invalid writes are skipped without changing fencing.
The same guide documents isolated HTTP burst diagnostics with mixed queries and
multiple API workers; these measurements include engine-counter instrumentation.
Search rejects timed-out or failed-shard results and reports Elasticsearch outages
with a sanitized 503; see [search failures](docs/search-failures.md).
Invalid or non-finite retrieval scores also fail before fusion/caching; valid
score ranking remains unchanged.
Public deployment hardening and the underlying transport cause of the measured
resolution delay remain unresolved; an opt-in connection workaround is available.

Inspect registered Python source without reindexing:
`.venv/bin/python -B -m scripts.audit_python_sources`
(optionally `--repository-id ID`). See [source audit](docs/source-audit.md) for
finding categories and grammar/snapshot limitations.
