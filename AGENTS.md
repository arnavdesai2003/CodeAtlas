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


## Latest milestone: coordinated full-index recovery (2026-10-01)

Full symbol/Elasticsearch indexers share the incremental-sync PostgreSQL advisory
lock via `app/indexer/locking.py`. `repository_full_index_jobs` is an additive
table created by startup; it was created in the local database during verification.
Symbol replacement and its job commit together; retries preserve committed IDs
without reparsing. Standalone full Elasticsearch indexing also journals work
before writes. Sync refuses pending full work; full indexing refuses pending sync.
Full publication deletes all repository documents, including stale symbol IDs,
then indexes the committed snapshot, strictly rotates cache generation and removes
the job. Empty snapshots still delete/invalidate. Failures leave replayable work.
Full rebuild does not advance the Git checkpoint or discover new files. Batch
failures return nonzero. Upgrade all writer processes together; legacy writers
are not coordinated. See `docs/full-index-recovery.md`.

73 offline tests pass. Live PostgreSQL verified lock exclusion across commit and
release after exceptions. An isolated real Elasticsearch/Redis smoke test with
SQLite metadata and deterministic vectors verified stale IDs, repository isolation,
retry after injected invalidation failure and empty-snapshot replacement; test
artifacts were removed. Live evaluation reproduced every baseline metric with 25
valid cases, hybrid Recall@10 .880 and MRR .499. The live corpus was not rebuilt.
Docker Desktop and existing containers were started; API processes were not
restarted. No benchmark or new performance claim. Publication remains in place
and can expose missing/mixed documents. Next priority: staging-index recovery
and atomic publication, including existing physical-index migration and consistent
parallel retrieval branches. Cache-miss coalescing remains separate.


## Latest milestone: staged atomic full publication (2026-10-01)

Full Elasticsearch rebuilds now copy unaffected repositories to a fresh whole
corpus index, add the target's committed symbols, validate/refresh, and switch
`codeatlas_symbols_active` atomically. Legacy physical `codeatlas_symbols` is
retained and used until the first publication. Both parallel hybrid branches
resolve one concrete generation per request; incremental writers resolve the
current generation but still update it in place. Effective source mappings and
analyzers are copied for staging; model, ranking and candidate sizes are unchanged. No new external infrastructure was added.

`index_publication_jobs` is an additive singleton journal created by startup;
it was created in the development database during verification. Normal writers
hold shared corpus plus exclusive repository session locks; full publication
holds exclusive corpus plus repository locks. A pending journal blocks all
cooperating writers across crashes except the owning repository's full-publication
retry. Stages progress building → ready → published → strict cache rotation →
job deletion. Retry inspects an ambiguous alias outcome and never rebuilds an
already active stage. A building retry uses a fresh random name because a
timed-out ES operation may still run. Retired/abandoned generations are retained;
there is no automatic cleanup or rollback protocol. Do not delete journals or
manually change aliases during recovery. Resume the owner with
`scripts.index_elasticsearch --repository-id ID` (default 1). Upgrade/restart all
API/writer processes together before publication; old processes are not fenced.
See `docs/atomic-publication.md` for limits and operator workflow.

96 offline tests pass. Live PostgreSQL verified shared/exclusive exclusion across
commits. Isolated real ES/Redis with SQLite metadata and deterministic vectors
verified alias migration/switches, old source preservation, stale IDs, repository
isolation, copied vector search, empty replacement, Redis fencing and lost-ack
recovery; test indices/aliases/keys were removed. All 25 evaluation cases valid,
every metric on the existing live index reproduced, hybrid Recall@10 .880 /
MRR .499. A real-corpus scratch publication rebuilt 40 micrograd symbols with CPU
embeddings and copied 4,300 other documents. All BM25/hybrid/reranked metrics
matched; standalone semantic Recall@10 .840 / MRR .581 versus live .880/.585.
An initial scratch copy before publication itself measured semantic .800/.561;
effective vector mappings were identical. Approximate index rebuilding shows
quality sensitivity; do not claim exact semantic preservation. Scratch indices
were removed; live corpus/routing/cache stayed unchanged. Existing live index
count was 4,340; it was not rebuilt/migrated for verification. Normal port 8000
was not restarted; temporary port 8001 servers were stopped. `.env` and default
device/thread settings stayed unchanged.

Matched uncached CPU/one-thread HTTP runs show a material single-worker latency
regression: before/control 19.228/18.828 ms avg versus after 36.976/38.018 ms.
Ten-worker after throughput 153.04/150.68 req/s versus before/control
162.85/171.65. All four runs had 800 successes and zero hits. Metadata-only alias
resolution averaged .426 ms, but interleaved engine diagnostics measured about
15 ms; the underlying transport/scheduling cause remains unresolved. This
records a consistency tradeoff, not a performance gain. Full details and
limitations: `docs/performance.md`. Full publication is atomic in Elasticsearch
per repository; batches, metadata/cache visibility and incremental sync remain
non-atomic. Next priorities: safe generation inspection/retention and investigation
of the name-resolution dependency without weakening generation consistency.
Cache-miss coalescing remains a separate performance investigation.

## Latest milestone: reviewed generation retention (2026-10-01)

`search_index_generations` is an additive lifecycle audit table created by
startup; it was created in the local development database during verification.
Publication durably records stage UUIDs before copy/index work, published stages,
retired sources and abandoned tracked attempts. Retirement uses confirmation time,
not creation age; Redis/finalization retries preserve the first inactive time.
Recorded source/stage identity changes block a ready alias switch. Older ready
jobs can adopt observed source/stage identities conservatively. Unknown old
attempts and lost identity commits stay protected; never guess history from age.

`scripts.manage_index_generations inspect` is read only. `plan --output PATH`
creates a new reviewable JSON file without overwriting existing plans. Default
policy: 24 hours inactive, keep two newest physically present retired published
generations (minimum one hour/one retained). Active, legacy, every aliased index,
journal references, untracked/malformed names, unexpected states and unverified
identities/times stay protected. `apply --plan PATH --quiesced` is maintenance
only: stop all readers/API/benchmark and writer processes first; there are no
reader leases. The flag is an operator attestation, not proof of quiescence.
Global exclusive corpus advisory locking excludes cooperating writers across
audit commits. All pending sync/full/publication jobs and active/incompletely
inspected Elasticsearch write tasks block application. The tasks API is technical
preview in the tested Elasticsearch client; failures block deletion.

Apply revalidates cluster, exact UUID/lifecycle/protection for all candidates
before deletion and again before each index. It never expands the reviewed set,
uses no wildcard deletes and retains audit rows. Partial/ambiguous outcomes exit
nonzero; an unchanged plan may reconcile absent same-identity/history targets.
Recreated indices or changed history require a new plan. No automated cleanup,
manual force-delete, reader-time changes or cross-store rollback added. Old,
external or direct-helper writers/manual alias changes remain outside locks;
keep the namespace quiescent. See `docs/index-generation-retention.md`.

122 offline tests pass. Live PostgreSQL verified maintenance exclusion of
shared writers/exclusive publishers, lock persistence across commits and release
on exceptions. Isolated real Elasticsearch with SQLite audit verified inventory,
UUID/statistics/alias/task APIs, exact reviewed scratch deletions, retained audit,
protected generations and idempotent retry. Isolated real Elasticsearch/Redis
publication recovery was rerun successfully with deterministic embeddings and
SQLite metadata. All test indices/aliases/keys were removed. Live read-only
inventory confirmed 4,340 documents, active protected legacy routing, no pending
jobs and no cleanup candidates. No live repository/audit rows, corpus/index,
cache, `.env`, default settings or running API processes were changed. Live
CPU/one-thread evaluation: all 25 cases valid, every baseline reproduced,
hybrid Recall@10 .880 / MRR .499. No new performance claim; the earlier
alias-resolution regression remains unresolved. Next priority: investigate that
resolution dependency while preserving per-request generation consistency.
Cache-miss coalescing remains separate; retained indices alone are not rollback.

## Latest investigation: resolution request sequencing (2026-10-01)

Added read-only `scripts.profile_resolution` with warm-up exclusion, failure exit,
separate-pool and predecessor/endpoint controls, and start/end routing/UUID/count
checks. CPU/one-thread run: alias avg .434 ms in a tight loop, 1.030 ms after
embedding, 16.161 ms after BM25 and 15.401 ms after hybrid. Info/count requests
also slowed after BM25; separate resolver pools did not remove the delay.
Tokenizer parallelism, Python switch interval and explicit IPv4 controls did not
demonstrate a fix. Instrumented wait was concentrated in socket response reads;
underlying server/transport/host cause remains unresolved. Details and limits:
`docs/performance.md`. These are component diagnostics, not HTTP measurements.

122 offline tests pass. Live routing/UUID/count unchanged, legacy physical index
with 4,340 documents. No production code, settings, ranking, corpus/index, cache,
metadata or API processes changed; no new retrieval-quality or performance gain
claim. Next targeted investigation: server/transport tracing after search,
including response size and connection behavior, while preserving per-request
concrete generation consistency. Cache-miss coalescing remains separate.

## Latest milestone: opt-in search connection workaround (2026-10-01)

Read-only `scripts.profile_search_transport` isolates response-size/connection
controls without loading models. Full BM25 responses averaged 75 KB and were
followed by 14–16 ms alias lookups; source-free controls averaged 2.7 KB/.617 ms.
Gzip did not remove the delay. Closing the preceding search connection reduced
following alias lookup to 1.150 ms with identical complete hits. Fresh alias
connections alone did not help. No unique TCP/Docker/server cause is established;
see `docs/performance.md` for diagnostics and limits.

Optional `ELASTICSEARCH_CLOSE_SEARCH_CONNECTIONS=true` closes only BM25/vector
search connections. Default false preserves deployment behavior; restart to
change it. Alias resolution/writers retain pooling, and both parallel branches
still pin one concrete generation. No source fields, ranking or candidates were
changed. CPU/one-thread matched uncached HTTP: before/default and closing
control averaged 38.783/40.221 ms at one worker; implemented opt-in runs
8.312/8.328 ms. Ten-worker opt-in throughput 145.35/143.68 req/s versus
154.16/158.25 controls; twenty-worker 133.48/144.26 versus 159.64/148.94.
The low-concurrency benefit has a throughput tradeoff; do not enable by default
or generalize to remote/TLS/other hosts. Four matched runs had 3,200 successes,
zero failures and zero cache hits; an extra header experiment is recorded
separately. No warm-cache or sustained-capacity improvement claim.

124 offline tests pass. Opt-in evaluation: all 25 valid cases, every baseline
metric reproduced, hybrid Recall@10 .880 / MRR .499. Read-only live routing/UUID/
count unchanged at legacy 4,340 documents. `.env`, default settings, corpus,
cache and existing port 8000 unchanged; temporary port 8001 server stopped.
No infrastructure added. Next investigation: identify the transport cause or
validate workload-specific choices before changing defaults. Cache-miss
coalescing remains separate.

## Latest investigation: host forwarding isolation (2026-10-01)

Added read-only `scripts.profile_forwarding`: curl search/alias pairs on host,
container loopback and container through `host.docker.internal`. Same ten
production BM25 queries/40 candidates, full results, four reuse/close blocks,
20 measured pairs after five warm-ups per block. Reused alias response wait
averaged 16.170/19.500 ms on host, .148/.141 ms on container loopback and
12.384/12.453 ms through host forwarding using the same container curl binary.
Connection preparation was tiny; the penalty followed it. Forwarded container
close controls had ~207 ms p95 waits, reinforcing route-specific limits of the
opt-in workaround. All 240 measured pairs completed with expected HTTP statuses
(search 200/absent alias 404); these are curl/component timings, not API capacity.

The forwarded path reproduces the delay without Python or embeddings; no unique
TCP/kernel/proxy mechanism established. Packet capture could not access macOS
BPF; noninteractive sudo required a password. No trace or kernel/Docker changes.
Diagnostic validates failures, statuses, curl-version timing quirks, connection
reuse and start/end routing/UUID/count. See `docs/performance.md` for full results,
compatibility fixes, preliminary controls and limitations.

124 offline tests pass. Live legacy routing/UUID/4,340-document count unchanged.
No app/default/.env/corpus/index/cache/schema/container configuration or API
process changed. Existing opt-in flag remains false by default; no new retrieval
quality or API performance claim. More precise attribution needs privileged packet
or forwarding/server tracing; do not change defaults based on these component
results. Cache-miss coalescing remains separate.

## Latest milestone: bounded process-local miss coalescing (2026-10-01)

Normal cache misses now share work for exact query text/limit/known Redis
initial generation within one API process. One leader retrieves and fills with
its original token; followers get independent result copies. A second leader
read avoids late duplicate fills but never adopts a newer token. New generations
cannot join old work. Completed flights are removed; no additional result cache
or distributed lock. Registry maximum 128 keys; one-second follower wait budget
then independent retrieval, without canceling/removing the leader. Saturated
new keys retrieve independently. Unknown-generation and benchmark-bypass
requests never join. Leader failures release followers and allow later retry.

`POST /search` adds `cache_coalesced` and `coalescing_wait_ms`. Followers are
`cache_hit=false`, have null engine timing and include their own waiting in
search latency. Bypass client rejects coalesced responses. Existing Redis Lua
fencing, normal cache-key normalization, retrieval text, ranking/candidates and
index consistency remain unchanged. See `docs/cache-coalescing.md`.

138 offline tests pass. Isolated real Redis verified old-leader/follower release,
new-generation independent retrieval, old fill rejection and fresh entry retention;
all temporary keys removed. Direct-service simultaneous-miss diagnostic (not
HTTP/capacity), 20 callers × ten queries × two rounds: before/control 400 engine
calls for 400 successes; after A/B 20 engine calls, 380 shared followers, zero
Redis hits. Average caller ms 126.475/124.690 before/control versus 37.108/37.481
after; 95% engine-call reduction is specific to this same-query burst workload.
`scripts.benchmark_miss_burst` uses isolated random Redis namespaces, matching
payload/cache checks and cleanup before final success reporting.

Four matched warm HTTP runs had 3,200 successes, 100% hits; throughput varied,
so no warm-cache improvement claim. Current guarded uncached HTTP had 800
successes, zero hits/coalesced responses; no uncached speedup claim. Details:
`docs/performance.md`. Temporary port 8001 server stopped; port 8000 untouched.
CPU/one-thread evaluation reproduced every baseline, 25 valid cases, hybrid
Recall@10 .880 / MRR .499. Read-only counts: six repositories / 4,340 PG symbols /
4,340 ES documents, legacy routing. No reindexing, schema, infrastructure,
ranking, .env or default device/transport changes. Normal HTTP warm-up may
populate/refresh benchmark cache entries; no live flush/generation rotation.
Coalescing is process-local, not a global capacity limit; slow leaders can trigger
fallback duplication. Cross-process sharing and the precise forwarding-delay
mechanism remain separate investigations; privileged tracing was unavailable at
the prior milestone. Atomic metadata/cache/index visibility remains unresolved.

## Latest milestone: HTTP mixed-miss and worker diagnostics (2026-10-01)

`scripts.benchmark_http_miss_burst` owns an isolated loopback server on 8001,
warms every worker and counts real engine executions in a random private Redis
namespace. It rejects occupied ports, validates loaded source/worker participation,
response attribution/equality and unchanged index identity/count. It stops the
server and removes only its private keys on success/failure. Never deploy its
diagnostic server factory. See `docs/cache-coalescing.md` for commands.

Eight instrumented HTTP runs compared archived pre-coalescing `f194b69` with
`fd7e566`, CPU/one-thread, pooled search connections, one/two API workers.
Each run released 20 requests in five bursts for each 1/2/5/10-query mix.
All 3,200 requests succeeded. One-worker after calls per 100 requests: 5/10/25/50
in both runs. Two-worker after calls: 10/19/39/72 and 10/17/45/60. Requests
arriving after a fill can hit Redis. One-query two-worker bursts needed two
leaders; ten-query benefit varied with worker assignment and was small in one
run. Redis counter/middleware overhead affects both versions, paid more often
by baseline; these are not standard uncached HTTP throughput or sustained capacity.
Full paired timing/count comparisons are in `docs/performance.md`.

142 offline tests pass. Only diagnostic code/docs changed; retrieval evaluation
from the preceding milestone remains inherited. All runs verified protected
legacy routing, same index UUID and 4,340 documents. Temporary servers stopped,
private namespaces removed; port 8000, live generation, corpus/index, schema,
ranking, .env/defaults untouched. Keep coalescing process-local: synthetic bursts
alone do not justify distributed coordination. Next priority: inspect search/API
failure behavior and deployment boundaries before further performance expansion;
privileged forwarding-delay tracing and atomic metadata/cache visibility remain
separate unresolved work.

## Latest milestone: complete-result search failures (2026-10-01)

Both retrieval branches set `allow_partial_search_results=false` and reject
responses with `timed_out` or `_shards.failed` before formatting/fusion/cache
fills. Hybrid does not return the surviving branch after a failure. Elasticsearch
API/transport errors (including alias resolution) and `IncompleteSearchError`
map to sanitized HTTP 503 `Search backend unavailable.`; unexpected model/code
errors remain ordinary 500s. Existing transport retries/timeouts, Redis fallback
and coalescing error propagation/retry remain unchanged. Existing valid cache
hits can succeed during an ES outage. No new deadline or automatic API retries.
See `docs/search-failures.md`.

145 offline tests pass. CPU/one-thread live evaluation: 25 valid, zero invalid,
every baseline metric reproduced, hybrid Recall@10 .880 / MRR .499. One guarded
uncached HTTP run: 800 successes, zero cache hits/coalesced responses; single
unmatched verification, no speedup claim. Initial server-readiness failure sent
no measured requests. Timing and methodology are in `docs/performance.md`.
Temporary port 8001 server stopped; port 8000, live cache generation, corpus,
index/schema, .env/defaults untouched. Public deployment authentication and
repository mutation failure disclosure remain separate priorities.

## Latest milestone: fail-closed webhook verification (2026-10-01)

GitHub webhook verification now rejects unset/empty secrets with HTTP 503 before
signature parsing, JSON or DB work. An empty-key forged signature previously
passed. Missing/invalid/non-ASCII signatures return 401; authenticated malformed
JSON/non-object roots and malformed push repository/clone URL fields return 400
without scheduling sync. Registered valid pushes, ping/ignored events, existing
background sync, journaling and locks retain their behavior. Acceptance is not
completion. See `docs/webhooks.md`.

153 offline tests pass, including eight mocked webhook authentication/scheduling
checks. No live webhook deliveries, database writes, clones, corpus/cache/index,
.env/defaults or API process changes. Retrieval evaluation remains inherited
from the prior milestone; no performance claim. This does not add public API
authorization, replay deduplication, durable queues or request body bounds.
Repository mutation error disclosure and deployment boundaries remain priorities.

## Latest milestone: typed repository mutation failures (2026-10-01)

Creation and sync routes return stable messages without raw Git stderr, clone
paths or backend exception details. Known failures have typed exceptions:
creation invalid URL 400, registration/directory conflict 409, Git failure 502;
sync absent repository 404, expected clone missing 409, coordination/pending
full-index work 409. Other failures stay 500; internal ValueError/FileNotFoundError
are no longer mistaken for expected user conditions. Malformed URL parsing is
classified before clone/database work. Successful schemas are unchanged, including
creation local_path. Direct callers retain detailed causes and previous built-in
exception family compatibility. See `docs/repository-errors.md`.

156 offline tests pass, including new API statuses/disclosure/invalid-URL checks
and all existing transaction/recovery/locking tests. Rollback, reservations,
ambiguous-commit clone retention and journal replay are unchanged. No live
ingestion/sync, data/schema/index/cache, .env/defaults or process changes; no
performance claim and retrieval evaluation remains inherited. Public API
authorization and request/resource boundaries remain separate priorities.

## Latest milestone: unambiguous repository URL inputs (2026-10-01)

Repository parsing now accepts plain HTTP/HTTPS GitHub/www.github.com URLs with
two ASCII path components and optional single trailing slash/.git suffix.
Credentials, explicit ports, query/fragment suffixes, percent escapes,
backslashes, repeated separators, whitespace/controls and non-ASCII components
are rejected before DB lookup/mkdir/Git work. Accepted text/case remains stored
unchanged; no canonicalization, migration or duplicate merging. See the precise
CodeAtlas input profile in `docs/repository-errors.md`.

158 offline tests pass. Read-only compatibility check: all six registered URLs
accepted, no rejected IDs; no URLs/credentials printed. No live ingestion/sync,
row/clone, corpus/index/cache, settings or process changes. Pending sync work
still resumes before URL parsing; old unsupported stored URLs need inspection
before new sync. Retrieval/performance measurements remain inherited. This
does not constrain redirects, Git credential/config behavior or source-file
symlinks. Filesystem/execution boundaries remain the next concrete inspection
priority; public authorization remains separate.

## Latest milestone: source symlink exclusion (2026-10-01)

Discovery/full symbol parsing/incremental sync share regular-file selection:
exclude absolute/traversal/.git paths, source symlinks and symlinked directories
inside clones, including internal/dangling links. Reads/hashes follow selection.
Full symbol replacement skips excluded sources and clears old symbols; affected
excluded supported paths in sync remove old file metadata/symbols and stay in
the durable affected-path journal for Elasticsearch deletion. Missing/non-regular
affected files are reconciled too. Deletion failures remain replayable, and
checkpoint/cache requirements are unchanged. See `docs/source-paths.md`.

162 offline tests pass, including symlink discovery, full replacement, sync
stale removal and failure/replay fixtures. CPU/one-thread live evaluation: 25
valid cases, every baseline reproduced, hybrid Recall@10 .880 / MRR .499.
Live corpus was not rebuilt; no live sync/data/schema/cache/settings/process
changes or performance claims. Old committed pending work/content is not scrubbed
automatically. Checks assume stationary clones/trusted roots, not race-free opens;
hard links, symlinked ancestors above clones, Git configuration/redirects and
public authorization remain separate boundaries.

## Latest milestone: clone-directory redirection checks (2026-10-01)

Ingestion/new sync/full symbol replacement share clone_directory validation.
Configured root, owner and clone must not be symlinks (including dangling) or
non-directory objects. Missing paths remain allowed for atomic ingestion
reservation; existing missing-clone errors remain. Unsafe paths return sanitized
409 on creation/sync. No automatic cleanup/repair. Pending journal replay stays
before filesystem checks and does not reread source. Existing locks, rollback
and clone retention remain unchanged. See `docs/source-paths.md`.

165 offline tests pass, including redirected-owner ingestion and redirected-clone
sync/full rejection before Git/parsing, external-file preservation, all component
levels and API disclosure/status checks. Live CPU/one-thread evaluation reproduced
all metrics on 25 valid cases, hybrid Recall@10 .880 / MRR .499; live corpus not
rebuilt. No live ingestion/sync, data/cache/schema/settings/process changes or
performance claims. Ancestors above configured root are trusted; validation is
not race-free against hostile local replacements. Git execution configuration
and public authorization remain separate inspection priorities.

## Latest milestone: Git command timeout and terminal-input policy (2026-10-01)

Clone/commit/branch/fetch/reset/diff share git_output: argv execution, stdin
DEVNULL, GIT_TERMINAL_PROMPT=0, per-command GIT_TIMEOUT_SECONDS (default 120,
positive integer). Other environment/config is preserved. Clone timeouts use
existing pre-commit cleanup and sanitized 502; sync timeouts propagate through
rollback/lock release with stable 500 and no checkpoint advancement. Ambiguous
commit retention and pending-publication-first replay are unchanged. A prior
reset may change worktree; retry uses existing checkpoint workflow. See
`docs/git-execution.md`.

168 offline tests pass, covering runner args/env/policy and ingestion/sync timeout
recovery plus existing failures. No live Git/clone/sync, data/index/cache/schema,
.env or API process changes. New setting documented in .env.example; quality
and performance measurements remain inherited. Timeout is per direct subprocess,
not whole-request/process-tree supervision; helpers holding pipes, askpass/config,
redirects and public authorization remain separate limits.

## Latest milestone: streaming HTTP body bound (2026-10-01)

Assembled app uses pure ASGI RequestBodyLimit before all HTTP route work.
REQUEST_BODY_MAX_BYTES defaults to 1 MiB, positive integer; restart to change.
Actual received chunks count, not Content-Length. Oversize returns stable 413
before parsing/signatures/DB/search/scheduling. Accepted bodies replay exact bytes;
signed webhook whitespace remains valid. Disconnect does not dispatch; lifespan
passes through. The size limit precedes route authentication including unconfigured
webhooks. Large legitimate deliveries need explicit higher limits. See
`docs/request-limits.md`; .env.example documents the setting, .env unchanged.

175 offline tests pass. Isolated one-worker CPU/one-thread HTTP smoke: 80
successful small-body searches, verified attribution/equality. Server stopped,
private namespace removed; index UUID/legacy routing/4,340 docs unchanged.
No live generation/corpus/schema/settings/port 8000 changes. Quality evaluation
is inherited; no performance claim. Bound covers middleware body accumulation,
not aggregate concurrency/transport buffers or receive deadlines. Public API
authorization, rate/concurrency limits and slow-client policy remain separate.

## Latest milestone: search query input bound (2026-10-01)

SearchRequest query is now 1–4,096 Unicode characters and must contain
non-whitespace text. Invalid HTTP input returns 422 before cache/coalescing/
retrieval; OpenAPI publishes maxLength. Accepted text is returned unchanged,
including surrounding whitespace/case/Unicode. Existing cache normalization,
exact-text flights, ranking/candidates/model and result limits are unchanged.
This is an API profile, not a model token limit; direct engine/service callers
retain existing policy. Body-size 413 takes precedence. See `docs/request-limits.md`.

178 offline tests pass, with boundary/Unicode/whitespace/no-service/OpenAPI checks.
No live data/cache/corpus/schema/settings/process changes; prior quality and
HTTP measurements remain inherited. No performance claim. Public authorization,
aggregate concurrency and slow-client handling remain separate priorities.

## Latest milestone: HTTP body receipt deadline (2026-10-01)

RequestBodyLimit uses one asyncio timeout for total receipt, default
REQUEST_BODY_TIMEOUT_SECONDS=30 (finite positive). Chunks do not reset the timer;
it ends before route dispatch. Expiry returns stable 408 without route work;
oversize observed first remains 413. External cancellation propagates. Search/
sync/Git/ES execution is unchanged. Restart to change setting; .env.example
updated, .env untouched. See `docs/request-limits.md` for slow-upload limits.

181 offline tests pass: partial-body cancellation/no dispatch, execution outside
timer and external cancellation, alongside size/signature coverage. No live
data/cache/corpus/schema/settings/process changes; quality/HTTP checks inherited,
no performance claim. Aggregate concurrency, public authorization and proxy/
server connection policies remain separate priorities.

## Latest milestone: real-socket body receipt verification (2026-10-01)

scripts.verify_http_receipt starts an isolated FastAPI fixture using production
body middleware on an ephemeral loopback port (16 bytes/.2 seconds). No stores,
models or production routes. Six cases passed: partial fixed/chunked uploads 408,
oversized fixed/chunked 413, exact-limit fixed/chunked 200 with unchanged bytes.
Handler counter proves rejections did not dispatch; fresh requests work after
each case. Server stopped automatically. See docs/request-limits.md for commands
and measured observations; never deploy the probe factory.

181 offline tests pass. Diagnostic code/docs only; no live data/cache/settings/
API processes changed, no new retrieval or performance claim. This verifies
fresh connections, not same-connection reuse/proxies or hard scheduling bounds.
Public authorization and aggregate concurrency remain separate priorities.

## Latest milestone: rejected HTTP upload connection closure (2026-10-01)

Body middleware sends Connection: close on early 408/413 for HTTP/1.0/1.1,
without draining stalled/unread uploads. Clients reconnect after rejection;
accepted requests retain keep-alive. HTTP/2 scopes omit connection-specific
headers, leaving stream behavior to the server. See docs/request-limits.md.

182 offline tests pass. Isolated socket probe: eight cases pass, all rejected
fixed/chunked bodies return close+EOF, including unfinished oversized uploads;
no rejected handler work, fresh connections healthy, accepted upload/stats reuse
same connection successfully. Probe stopped; no stores/settings/production
process changes or performance claim. HTTP/2 checked only via ASGI header test,
not real transport/proxy. Public authorization/aggregate concurrency remain
separate priorities.

## Latest milestone: single-key API access boundary (2026-10-01)

Search/repository router now requires X-CodeAtlas-API-Key when API_KEY is set
in any environment. Without a key, only development/test remain unauthenticated;
all other APP_ENV values fail closed 503. Wrong/missing configured key returns
401 before services/DB. SecretStr settings redaction and constant-time bytes
comparison; no keys printed/committed. Health/root/docs public; webhooks retain
independent signatures. Body limits precede authentication. Valid key never
overrides guarded benchmark bypass. See docs/api-access.md.

187 offline tests pass, including all protected operations, nonlocal defaults,
valid/invalid keys, health and benchmark guard behavior after authentication.
No live data/cache/settings/process changes; .env untouched and .env.example
documents optional key. Retrieval/performance checks inherited. Key grants all
operations, no scopes/multi-key rotation/TLS/rate limits/identity infrastructure.
Restart every process together for rotation; local benchmarks assume unset key.

## Latest milestone: explicit keyed local benchmark clients (2026-10-01)

benchmark_api and benchmark_http_miss_burst use explicit client env
CODEATLAS_BENCHMARK_API_KEY, never automatically server API_KEY/.env credentials.
Unset/empty remains keyless. Keyed targets require HTTP/HTTPS loopback/localhost
without URL credentials; redirects/proxy env disabled. Printable ASCII/no
whitespace client keys; errors do not echo secrets. Client header merges with
guarded bypass headers; server restrictions remain. See docs/api-access.md.

190 offline tests pass. Isolated keyed one-worker CPU/one-thread HTTP smoke:
generated key in process env only, 80 successful searches with verified
attribution/equality. Server stopped/private keys removed; legacy routing/UUID/
4,340 docs unchanged. No .env/live cache generation/port 8000 changes, no new
retrieval/capacity claim. Archived matched controls need compatible key policy.

## Latest milestone: Git child repository environment isolation (2026-10-01)

git_output removes inherited GIT_DIR/COMMON_DIR/WORK_TREE/INDEX_FILE,
OBJECT_DIRECTORY/ALTERNATE_OBJECT_DIRECTORIES, NAMESPACE/PREFIX/GRAFT_FILE/
SHALLOW_FILE/REPLACE_REF_BASE only from child environment. Parent variables,
credential helpers/config and unrelated settings remain untouched; terminal
prompt policy/timeout unchanged. See docs/git-execution.md for precise list/limits.

192 offline tests pass. Mocked all-variable policy and real two-temporary-repository
Git fixture confirm -C selects requested clone despite conflicting parent
repository/worktree/index variables and creates no external index. Fixtures
removed; no network/live clone/sync/data/cache/settings/process changes or new
quality/performance claims. User/local/system/config injection remains outside
this scoped isolation. Missing clone Git metadata/discovery remains next inspection
priority; public authorization scopes/concurrency remain separate.

## Latest milestone: clone-owned Git metadata (2026-10-01)

New sync and ingestion commit/branch reads require an unlinked .git directory
with no commondir redirects; .git files/links/missing directories rejected.
Repository commands pass absolute --git-dir/--work-tree as well as -C,
preventing parent discovery. This supports ordinary generated shallow clones,
not linked/shared worktrees. Unsafe metadata uses existing sync409; no automatic
repair. Initial ingestion inspection failures retain pre-commit cleanup. Pending
sync publication still resumes before checks; full symbol snapshot does not
invoke Git. See docs/git-execution.md.

196 offline tests pass, including real temporary Git chosen/parent fixtures,
no-new-sync mutation on missing metadata and successful pending replay without it.
Read-only compatibility audit: all six registered clones accepted; no paths/URLs
printed. No live Git/network/sync/data/cache/settings/process changes or new
quality/performance claim. Metadata-entry symlinks/integrity, malicious config
and concurrent filesystem replacement remain outside this scoped layout check.

## Latest milestone: lossless incremental Git diff records (2026-10-01)

Sync now requests --name-status -z and requires strict NUL records. Decoded
tabs/newlines/whitespace/quotes/Unicode paths preserved exactly; renames keep both
paths, copies add destination without removing source, A/M/D/T preserved.
Malformed/unknown/truncated records fail before reset/metadata/checkpoint work.
Standalone line-format parser remains compatible; production no fallback.
Committed journals replay unchanged. See docs/git-execution.md.

201 offline tests pass, including real temporary Git tab/newline fixture,
exact sync metadata/deletion paths and malformed-diff no-reset/checkpoint checks.
Live CPU/one-thread evaluation: 25 valid, all baselines reproduced, hybrid
Recall@10 .880 / MRR .499. No live sync/reindex/data/cache/settings/process changes,
no performance claim. Undecodable filename bytes fail instead of lossy decode;
binary filename support remains separate.

## Latest milestone: real Git type-change recovery (2026-10-01)

Existing metadata updated by T status now counts files_modified alongside M;
newly eligible regular files still count added, excluded replacements deleted.
Selection/publication/ranking unchanged. New real local Git fixture covers
commits/refs/NUL diff/reset through regular→symlink→regular lifecycle with
temporary SQLite and mocked fetch/ES/Redis, including deletion-failure journal
retention, old checkpoint, resumed publication, restored symbols and intact
external target. See docs/source-paths.md.

203 offline tests pass. Temporary artifacts removed; no live sync/clone/data/
cache/settings/process changes. Quality evaluation inherited from preceding
diff milestone; no new quality/performance claim. This proves local Git lifecycle
with mocked external stores, not live cross-store atomicity or network recovery.

## Latest milestone: rename/copy publication replay coverage (2026-10-02)

205 offline tests pass. Real local Git rename fixture with tab/newline destination
verifies refs/NUL diff/reset, both exact journal paths, removed old metadata,
stored new symbols, retained checkpoint after publication failure, and resumed
publication without Git calls. Test explicitly enables rename detection; actual
production configuration unchanged. Deterministic C100 fixture verifies copy
source file/symbol identity preservation, destination-only journaling, added vs
renamed counts, and retry without reparsing. See docs/source-paths.md.

Tests/docs only; no production fix needed. Fetch/ES/Redis mocked, temporary Git
and SQLite fixtures removed. No live repository/data/cache/settings/process
changes or new quality/performance claim. Copy test consumes C100, not automatic
Git copy discovery. Prior evaluation remains inherited.

## Latest milestone: encoding-aware Python source reads (2026-10-02)

Full/incremental symbol parsing share read_python_source via tokenize.open,
honoring coding declarations/UTF-8 BOM and failing on invalid encodings rather
than replacement-decoding. Unicode enters existing UTF-8 Tree-sitter parser;
raw hashes/newline semantics/language scope unchanged. Decode failures roll back
metadata/symbol replacement, preserve checkpoint and create no new journal.
Pending jobs replay stored symbols unchanged. Fix invalid source upstream;
do not edit generated clones. No automatic rebuild of older lossy code. See
docs/source-paths.md for reconciliation/limits.

210 offline tests pass, with Latin-1/BOM/cookie/error and sync/full rollback checks.
Live CPU/one-thread evaluation: 25 valid, every baseline reproduced, hybrid
Recall@10 .880 / MRR .499. Live corpus not rebuilt; no claim about quality after
re-decoding existing content. No live data/cache/settings/process changes or
performance claim. Ranking/publication unchanged.

## Latest milestone: per-call Tree-sitter parser isolation (2026-10-02)

parse_python_source constructs its own Parser; no module-global mutable parser
shared across different repository writers. Language definition/traversal/output
unchanged, no global lock or thread-local cache. Removes shared parser state;
no historical corruption reproduced. See docs/source-paths.md.

212 offline tests pass, including synchronized six-instance concurrent extraction
with source-specific names/code/lines and failed-instance recovery. Live CPU/
one-thread evaluation: 25 valid, all baseline metrics reproduced, hybrid
Recall@10 .880 / MRR .499. Live corpus not rebuilt; no data/cache/settings/process
changes or performance claim. Per-call construction adds allocation; ingestion
performance not measured. Repository writer lock boundaries remain unchanged.

## Latest milestone: incremental bulk completeness guard (2026-10-02)

Incremental ES indexing now validates bulk returned errors and exact success
count against actions before refresh/invalidation/checkpoint finalization,
matching full indexing. Exceptions retain existing propagation. Defensive guard;
ordinary item failures already raise by default and no silent live failure was
observed. Pending sync preserves committed IDs; replay re-deletes/reindexes
without reparsing. Empty-snapshot deletion behavior unchanged. See docs/sync-recovery.md.

213 offline tests pass, including short/error/excess bulk returns, retained
job/IDs/checkpoint, no refresh/cache invalidation, then successful replay.
Live CPU/one-thread evaluation: 25 valid, every baseline reproduced, hybrid
Recall@10 .880 / MRR .499. No live sync/reindex/data/cache/settings/process
changes or performance claim. Publication remains non-atomic across stores.

## Latest milestone: webhook background session lifecycle (2026-10-02)

Background SessionLocal creation is inside error handling, with conditional
finally close; ordinary close failures are contained/logged too. Replaced raw
prints with app.api.webhooks logging: completion INFO, failure/close ERROR,
repository ID and exception type only, no raw exception/result/traceback.
Completion plus close failure means sync succeeded but cleanup failed; no claim
that failed close releases resources. Existing sync rollback/locks/journals
unchanged. See docs/webhooks.md for logging and retry limits.

216 offline tests pass, including session creation/sync/close failure containment
and sanitized captured logs. No live delivery/sync/data/cache/settings/process
changes or new quality/performance claims. Tasks remain in-process, not durable;
accepted response is not completion. Pre-journal failure requires retry, no
automatic queue/retry added. Process termination is outside ordinary exceptions.

## Latest milestone: bounded finite cache values (2026-10-02)

Cache reads reject over-limit lists/non-dict rows, NaN/Infinity and float overflow
including nested numeric values; miss keeps original generation. Writes check
shape/count and JSON allow_nan=False, skip invalid writes without Redis calls.
Valid empty/finite entries and source strings unchanged. Lua fencing/TTL/cache
fallback unchanged; no flush/schema migration. Not full field-schema or fresh
engine-output validation. See docs/cache-consistency.md.

219 offline tests pass. Isolated one-worker CPU/one-thread HTTP smoke: 80
successes including six Redis hits, same-query payload/attribution verified.
Server stopped/private keys removed; legacy routing/index UUID/4,340 docs
unchanged. No live generation/settings/process changes or performance claim.
Retrieval evaluation remains inherited; no ranking/candidate/index changes.

## Latest milestone: finite retrieval score boundary (2026-10-02)

Hit formatting rejects non-numeric/bool/NaN/infinity/overflow scores; null/missing
still zero. Normalization rejects non-finite values and overflowing ranges.
InvalidSearchResponseError uses existing sanitized503, no successful cache/shared
publication. Finite values/min-max/equal-score behavior/weights/candidates unchanged;
no bad-score fallback. Scope excludes full backend schema/optional reranker output;
no prior live invalid-score incident observed. See docs/search-failures.md.

222 offline tests pass. Live CPU/one-thread evaluation: 25 valid, all baselines
reproduced, hybrid Recall@10 .880 / MRR .499. Isolated one-worker HTTP smoke:
80 successes, payload/attribution verified, server/private keys cleaned; legacy
routing/index UUID/4,340 docs unchanged. No live corpus/settings/process changes
or performance improvement claim.

## Latest milestone: startup ranking/cache numeric validation (2026-10-02)

Settings now enforce finite HYBRID_SEMANTIC_WEIGHT in [0,1] (default .60),
positive SEARCH_CACHE_TTL (default300). Invalid env/direct construction fails
startup instead of request-time ranking/cache writes. Other existing numeric
constraints unchanged. Formatted Pydantic errors hide input values; programmatic
errors()/JSON not scrubbed. See docs/settings-validation.md.

227 offline tests pass with isolated defaults/endpoints/invalid/nonfinite/env/
formatted-error tests. Existing local load confirmed .60/300 without secrets.
No .env/data/cache/process changes, no quality/performance claim; measurements
inherited. No weight tuning; changed valid weights still require evaluation.

## Latest milestone: settings secret representation hygiene (2026-10-02)

Webhook secret now SecretStr like API_KEY; signature consumer explicitly unwraps
for HMAC. Empty-secret fail-closed behavior unchanged. DB/ES/Redis URLs stay plain
client strings but repr=False excludes from Settings str/repr. Webhook secret
JSON masks, raw model_dump/JSON service URLs remain sensitive; not encryption or
general scrubber. See docs/settings-validation.md. Restart settings+consumer
together to load types; direct key consumers use get_secret_value().

228 offline tests pass with representation/key JSON/type checks and existing
webhook signatures/unconfigured rejection. No actual secrets printed, .env/live
delivery/data/cache/settings/process changes. Quality/performance inherited.

## Latest milestone: read-only Python source audit (2026-10-02)

`scripts.audit_python_sources` checks registered Python files with the installed
Tree-sitter grammar, encoding-aware reads and existing source path exclusions.
Optional `--repository-id ID` restricts inspection. JSON findings distinguish
unavailable/excluded paths, source read errors and grammar recovery; findings
return nonzero. Unsafe clone boundaries and nonexistent selected repositories
abort. No Git, metadata, symbol, index or cache writes occur. Inspection is not a
locked snapshot or Python semantic validation. Tolerant production parsing stays
unchanged. See `docs/source-audit.md`.

Read-only live inspection: six repositories, 219 Python files, all clean.
230 offline tests pass. No new retrieval or performance measurements; prior
quality baseline remains applicable. Running APIs and settings were unchanged.

## Latest milestone: ambiguous sync commit coverage (2026-10-02)

Added fresh-session transaction tests for lost acknowledgements after successful
sync preparation and final commits. Preparation loss preserves the pending
target and symbol IDs; retry ignores advanced source/remote and replays without
Git or parsing. Final loss leaves the checkpoint advanced and journal absent;
unchanged-remote retry performs no repeated publication or invalidation. See
`docs/sync-recovery.md` for operator interpretation of ambiguous errors.

232 offline tests pass. No production changes were needed, no live stores or
running processes changed, and no new retrieval/performance claims were made.
SQLite tests exercise application recovery rather than PostgreSQL network loss.

## Latest milestone: ambiguous full-publication commit coverage (2026-10-02)

Added fresh-session tests for successful full symbol snapshot, ready stage,
published lifecycle and final journal deletion commits followed by lost
acknowledgements. Symbol IDs survive without reparsing; ready stages resume
without copy/index work; published stages finalize without a second alias switch
and retain the first publication timestamp. Completed final commits leave both
journals absent and published audit intact. A new full-index command after that
completion starts a new rebuild, rather than reconstructing the lost response.
See full-index/atomic-publication documentation before interpreting an error as
pending work. Tests use SQLite transactions and mocked Elasticsearch/Redis.

236 offline tests pass. No production behavior, live stores, settings or running
processes changed; no new retrieval or performance claims.

## Latest milestone: ambiguous retention audit coverage (2026-10-02)

Fresh-session tests now cover acknowledged index deletion followed by successful
audit commit and lost database acknowledgement. Cleanup stops before the next
candidate; unchanged-plan retry reconciles absence without rewriting the first
deletion timestamp. Recreated same-name/different-UUID indices block all retry
deletions despite the previously committed deleted audit row. Requirements for
quiescence, reviewed plans and maintenance locks remain unchanged. See
`docs/index-generation-retention.md`. Tests use SQLite and simulated ES identities.

238 offline tests pass. No production changes, live deletion, policy/settings
changes or new retrieval/performance measurements.

## Latest milestone: byte-preserving Git filenames (2026-10-02)

Fixed subprocess universal-newline conversion corrupting carriage returns and
CRLF sequences in NUL-delimited Git filenames. Git runner captures bytes and
decodes strict UTF-8 without newline rewriting; failed-command diagnostics retain
text with replacement decoding only for diagnostics. Real Git regression was
reproduced before the fix. Extended rename/recovery coverage preserves exact
CR/CRLF paths through metadata, journals and retries. See `docs/git-execution.md`.

240 offline tests pass. Live read-only CPU/one-thread evaluation reproduced all
baseline metrics with 25 cases, hybrid Recall@10 .880 / MRR .499. No live corpus
rebuild, cache/settings changes, API restart or new performance claim.

## Latest milestone: iterative symbol traversal (2026-10-02)

Replaced recursive Python Tree-sitter traversal with an explicit stack, preserving
preorder and existing class/function scope behavior. Reproduced RecursionError on
a valid 1,500-term binary expression before a function; extraction now succeeds.
No parsing rejection policy or source-size limits added. Read-only comparison of
old/new extraction across 219 local Python files matched every symbol field/order.
See `docs/source-audit.md` for limits and verification.

242 offline tests pass, including deep-tree and nested/sibling scope regressions.
Live CPU/one-thread evaluation: 25 valid cases, every baseline metric reproduced,
hybrid Recall@10 .880 / MRR .499. Existing corpus was not rebuilt; no cache,
settings or API process changes and no performance claim.

## Latest milestone: checked Elasticsearch refresh (2026-10-02)

Explicit refreshes after incremental/full bulk writes and final staging now
require integer `_shards.failed` zero. Failed or missing/invalid counts raise;
sync retains committed journals/IDs without checkpoint advancement or cache
invalidation. Staging remains building with no alias switch; retry uses a fresh
stage. Existing recovery/consistency limits remain. See sync/atomic-publication
documentation. No ranking or corpus changes.

245 offline tests pass. Isolated real Elasticsearch one-document refresh check
passed and its scratch index was removed. Live CPU/one-thread evaluation: all 25
cases valid, every baseline metric reproduced, hybrid Recall@10 .880 / MRR .499.
Live corpus/routing/cache, settings and running API processes were unchanged.
No new performance claim or induced live shard outage.

## Latest milestone: staging creation acknowledgement guard (2026-10-02)

Staging creation now requires both boolean acknowledgements before copy/index
work. Ambiguous, missing or malformed responses preserve the building journal;
retry abandons the tracked attempt and uses a fresh name without deleting the
potentially created index. An attempt without recorded UUID remains protected
under retention policy. Legacy provisioning is unchanged. See atomic-publication
documentation. No automatic cleanup or reconciliation added.

247 offline tests pass. Isolated real Elasticsearch staging creation and copied
mapping passed; both scratch indices removed. Live CPU/one-thread evaluation
reproduced all baseline metrics with 25 valid cases, hybrid Recall@10 .880 /
MRR .499. No live corpus/routing/cache, settings or API changes; no performance
claim or induced live creation timeout.

## Latest milestone: complete publication counts (2026-10-02)

All three full-publication count sites now require nonnegative integer counts
and explicit integer zero failed shards; timeout/partial/malformed responses
block publication. Building failures follow fresh-stage recovery; ready failures
retain the validated stage for revalidation without rebuild. Counts do not verify
content equality. See atomic-publication documentation. No ranking changes.

250 offline tests pass. Isolated real Elasticsearch empty/populated/filtered
count checks passed and scratch index removed. Live CPU/one-thread evaluation:
25 valid cases, every baseline metric reproduced, hybrid Recall@10 .880 /
MRR .499. No live corpus/routing/cache, settings or API changes; no performance
claim or induced live shard failure.

## Latest milestone: index embedding batch validation (2026-10-02)

Full/incremental bulk paths validate all vectors before submission: exact symbol
count, 384-dimensional lists and finite numeric values excluding booleans/strings.
Invalid output leaves recovery work pending; sync checkpoint/cache finalization
cannot proceed. Valid values are unchanged. Existing earlier deletion/staging
operations remain non-atomic; ES still checks mapping/similarity constraints.
See full-index-recovery documentation. No model/ranking changes.

253 offline tests pass. Two real CPU/one-thread model batch vectors passed the
validator without indexing. Live CPU/one-thread retrieval evaluation: 25 valid
cases and all baseline metrics reproduced, hybrid Recall@10 .880 / MRR .499.
No invalid live model output was observed, no corpus/routing/cache/settings/API
changes and no performance claim.

## Latest milestone: query embedding validation (2026-10-02)

Semantic search shares indexing's 384-dimensional finite numeric vector checks
before ES query submission. Invalid output raises typed InvalidQueryEmbeddingError
through existing sanitized 503 handling. Failures do not fill cache and release
miss flights for healthy retry; valid vectors are unchanged. Hybrid lexical work
can already be running, but partial branch fallback remains disabled. No new
query limits, normalization policy or model/ranking changes. See search-failures.

254 offline tests pass. Live CPU/one-thread evaluation: 25 valid cases, every
baseline metric reproduced, hybrid Recall@10 .880 / MRR .499. No invalid live
model output was observed; no corpus/routing/cache/settings/API changes or new
performance claim.

## Latest milestone: optional reranker score validation (2026-10-02)

Optional reranking validates exact candidate/score count and finite numeric scalar
scores before result construction/sorting. Malformed output raises typed
InvalidRerankerOutputError through existing sanitized backend-error handling;
NumPy scalar scores, stable ties, valid limits and copied candidate dictionaries
are preserved. Empty sets skip model loading; default API reranking remains off.
See search-failures documentation. No model/ranking policy changes.

258 offline tests pass. Live CPU/one-thread evaluation: 25 valid cases and every
baseline reproduced, including reranked Recall@10 .800 / MRR .493 and hybrid
Recall@10 .880 / MRR .499. No invalid live model output observed, no corpus/cache/
settings/API process changes and no performance claim.

## Latest milestone: serialized reranker cold initialization (2026-10-02)

Optional cross-encoder cached construction is now protected by a process-local
initialization lock, matching embeddings. Six cold callers share one model;
failed construction releases the lock and can retry. Prediction remains outside
the lock and concurrent. No global model pool or inference thread-safety claim;
each worker still loads separately. Model/device/thread settings and default
non-reranked API behavior remain unchanged. See search-failures documentation.

261 offline tests pass, with event/barrier initialization/failure/prediction
coverage. Live CPU/one-thread evaluation: 25 valid cases, all baseline metrics
reproduced, hybrid Recall@10 .880 / MRR .499 and reranked .800 / .493. No live
corpus/cache/settings/API process changes, measured cold-start incident or new
performance claim.

## Latest milestone: pruned, failure-aware discovery (2026-10-02)

Source discovery uses a top-down walker that prunes `.git` before descent and
does not follow linked directories. Existing regular-source checks/extensions/
hashes remain; linked roots yield no files. Directory scan errors now propagate
instead of silently committing a partial inventory. Ingestion rollback/cleanup
coverage verifies no repository/file commit after discovery failure. Existing
ambiguous-commit preservation and filesystem-race limits remain. See source-paths.

264 offline tests pass. Read-only old/new discovery comparison across six clones:
223 source-file paths, languages and hashes identical. Live CPU/one-thread
evaluation: 25 valid cases, all baseline metrics reproduced, hybrid Recall@10
.880 / MRR .499. No live ingestion/rebuild/cache/settings/API changes or new
performance claim; iteration order is not guaranteed.

## Latest milestone: cache nesting guard (2026-10-02)

Cached JSON list/dictionary nesting is bounded to 16 container levels including
the outer results list. Iterative validation rejects excessive/cyclic writes;
decoder RecursionError becomes a generation-bound miss. Healthy retrieval can
conditionally replace invalid entries with the same generation fence. No full
row schema/byte-size bound, key migration, rotation or flush added. Normal flat
rows, empty hits and benchmark bypass are unchanged. See cache-consistency.

268 offline tests pass. Isolated real Redis checked corrupt nested read fallback,
retained generation, healthy refill and TTL; both private keys removed. No live
search-cache/corpus/settings/API changes. Retrieval/ranking unchanged; previous
quality evaluation remains applicable, with no new evaluation/performance claim.

## Latest milestone: explicit Git commit resolution (2026-10-02)

Sync verifies the fully qualified remote-tracking ref as a commit rather than
ambiguous origin/branch shorthand. HEAD lookup also verifies commit identity;
diff separates revisions/pathspecs with trailing --. Reproduced a colliding
origin/main tag causing incorrect changed=false before the fix; real Git tests
now select the remote target and reject a tag-only substitute for missing remote
ref before mutation. Branch/fetch/checkpoint recovery policy unchanged. See
Git execution documentation.

270 offline tests pass. Read-only HEAD compatibility matched all six local clones.
Live CPU/one-thread evaluation: 25 valid cases, all baseline metrics reproduced,
hybrid Recall@10 .880 / MRR .499. No live fetch/reset, corpus/cache/settings/API
changes or new performance claim.

## Latest milestone: retrieval hit validation (2026-10-02)

Retrieval validates hit collections/source fields and line ranges before result
construction. Required strings, integer positive/ordered lines, optional nullable
language and boolean test flag are checked. Invalid hits raise typed existing
sanitized backend errors rather than lookup exceptions or malformed successes;
cache fill is skipped and miss-flight state released. Valid formatting/defaults,
scores and ranking unchanged. Existing Redis entries are not fully schema
validated by this change. See search-failures documentation.

273 offline tests pass. Live CPU/one-thread evaluation: 25 valid cases, all
baseline metrics reproduced, hybrid Recall@10 .880 / MRR .499. No malformed live
document observed/repaired, no corpus/cache/settings/API process changes or
performance claim. Existing coalescing timeout behavior needed no change.

## Latest milestone: API-key OpenAPI scheme (2026-10-02)

OpenAPI now declares CodeAtlasAPIKey header security on search/repository routes
for Swagger Authorize and client discovery. Configured values are never included.
Public health/root and independently signed webhooks have no API-key requirement.
Protected routes share an internal dependency-bearing router included in exported
API router. APIKeyHeader uses auto_error=False to preserve existing configured/
unconfigured environment policy and sanitized runtime responses. See api-access.

275 offline tests pass, including every protected schema operation, public
exclusions and secret absence, alongside existing runtime auth/bypass checks.
No retrieval/ranking change; previous evaluation remains applicable. Settings and
running APIs unchanged; existing servers need restart for the new schema.

## Latest milestone: strict incremental delete response validation (2026-10-02)

Incremental Delete By Query now requires explicit non-timeout completion, an empty
failure list, zero integer version conflicts, and matching nonnegative integer
total/deleted counts. Missing/malformed/partial responses raise before cache
invalidation/checkpoint finalization; committed sync jobs remain replayable. Retry
repeats idempotent path deletion and stored-ID indexing. No cross-store atomicity
claim. Updated the existing successful-response fixture to match ES response
shape. No tests were run or added for this change, per current agent instruction.

## Latest follow-up: incremental deletion fixture alignment (2026-10-04)

Static review found the published-generation incremental-write success fixture
still returned an empty Delete By Query response. Updated that existing fixture
to explicit successful completion with zero matching total/deleted counts, zero
conflicts and no failures, consistent with strict response validation. No new
tests added or tests run, preserving the prior milestone's recorded constraint.
Diff whitespace checks passed; no production behavior, live services, corpus,
cache or settings changed. The previously reported 275-test result predates the
strict deletion change and is not a validation claim for either follow-up.

## Latest milestone: strict staging-copy completion (2026-10-04)

Full publication now requires explicit non-timeout staging-copy completion, an
empty failure list and integer zero version conflicts. Total/created counts must
be nonnegative integers matching the checked source count; boolean/float numeric
equivalence no longer passes. Invalid responses stop before target indexing and
alias publication, retaining building work for fresh-stage retry. Existing
success/count-mismatch fixtures aligned; no tests added or run under the recorded
constraint. Diff whitespace checks passed. No live publication, corpus, cache,
settings/API changes or new quality/performance claim. See atomic-publication.

## Latest milestone: strict alias-switch acknowledgement (2026-10-04)

Alias publication now requires a mapping with literal acknowledged=true and,
when present, literal errors=false. Truthy strings/integers and malformed error
flags no longer authorize finalization. Older successful responses omitting the
optional errors field remain supported. Rejection retains ready work; existing
retry inspects the actual alias outcome, preserving lost-ack recovery without
rebuilding an active stage. No tests added or run under the recorded constraint;
diff whitespace checks passed. No live publication/service/settings/corpus/cache
changes or new evaluation/performance claim. See atomic-publication.

## Latest milestone: publication response container guards (2026-10-04)

Publication counts require mapping responses and shard metadata, with literal
timed_out=false when the optional field is present. Missing timeout remains
supported by the Count API contract; falsey malformed values no longer pass.
Staging creation also guards response shape before boolean acknowledgement
checks. Invalid containers raise existing publication RuntimeErrors, preserving
building/ready recovery instead of incidental attribute errors. No tests added
or run under the recorded constraint; diff whitespace checks passed. No live
services/publication/settings/corpus/cache changes or new metrics claimed.

## Latest milestone: refresh response container guards (2026-10-04)

Shared Elasticsearch refresh validation now checks response/shard metadata
mapping shapes before reading the failed count. Malformed containers raise the
existing incomplete-refresh RuntimeError instead of incidental attribute errors.
Incremental/full publication retain journaled recovery work and stop before
cache/checkpoint finalization or alias switching respectively. Valid integer
zero failure responses remain unchanged. No tests added or run under the
recorded constraint; diff whitespace checks passed. No live services, settings,
corpus, cache or publication changes and no new metrics claimed.

## Latest milestone: alias resolution response validation (2026-10-04)

Alias resolution requires a single-target mapping, nonempty string target and
mapping metadata containing the requested alias. Invalid/unrelated responses
raise instead of selecting an arbitrary iterable element or silently falling
back. Only NotFoundError retains legacy fallback. Per-request concrete generation
pinning and valid routing remain unchanged. Existing success fixtures aligned;
no tests added or run under the recorded constraint. Diff whitespace checks
passed. No live routing/services/settings/corpus/cache changes or new metrics.

## Latest milestone: symbol-index provisioning acknowledgement (2026-10-04)

Missing symbol-index creation now requires mapping responses with both literal
acknowledged=true and shards_acknowledged=true before writers continue. Invalid
or unacknowledged responses raise for retry without deleting the possibly created
index. Existing-index retry behavior remains; this does not validate mappings or
reconcile missing published generations. Initial legacy provisioning uses the
same check. No tests added or run under the recorded constraint; diff whitespace
checks passed. No live services/routing/settings/corpus/cache changes or metrics.

## Latest milestone: missing published-generation write guard (2026-10-04)

Symbol-index provisioning now creates only the legacy bootstrap index. Missing
nonlegacy targets raise before incremental delete/index writes instead of
recreating an empty published generation. Existing generations remain writable;
committed sync work remains retryable after operator reconciliation. This is an
existence-check guard, not protection against external deletion racing later
writes or Elasticsearch auto-creation. No tests added or run under the recorded
constraint; diff whitespace checks passed. No live routing/services/settings/
corpus/cache changes or new metrics claimed. See sync-recovery.

## Latest correction: Elasticsearch response wrapper compatibility (2026-10-04)

Static inspection of installed elastic_transport found ObjectApiResponse does
not implement Mapping, despite delegating dictionary access. Recent container
guards would reject valid real-client responses. Added shared explicit wrapper
unwrapping for alias resolution, refresh, creation, deletion, publication counts,
reindex completion and alias-switch checks. Validation now applies to JSON bodies;
plain dictionaries and strict field checks remain supported. This corrects the
preceding guards; their whitespace checks did not establish client compatibility.
No tests added or run under the recorded constraint. Diff whitespace checks
passed; installed source confirms the exported wrapper type/body property. No
live services/routing/settings/corpus/cache changes or new metrics claimed.

## Latest milestone: strict retention deletion acknowledgement (2026-10-04)

Reviewed generation cleanup unwraps Elasticsearch response bodies and requires
mapping/literal acknowledged=true before recording deleted audit state. Truthy
malformed values no longer authorize that commit. Rejection stops before audit
update/further candidates; existing absent-target identity/history retry can
reconcile an ambiguous deletion. No retention policy, quiescence or lock changes.
No tests added or run under the recorded constraint; diff whitespace checks
passed. No live cleanup/services/routing/settings/corpus/cache changes or metrics.

## Latest milestone: strict retention task inspection (2026-10-04)

Cleanup task inspection unwraps response bodies and validates mapping nodes,
node entries and task collections. Optional node/task failures require empty
lists; malformed falsey values cannot establish idle state. Empty nodes remains
accepted; malformed inspection or any write task blocks deletion. Existing
quiescence/locking requirements remain and external post-inspection writes are
not excluded. No tests added or run under the recorded constraint; diff
whitespace checks passed. No live cleanup/service/settings/corpus/cache changes
or new metrics claimed. See index-generation-retention.

## Latest milestone: complete retention statistics (2026-10-04)

Generation inventory unwraps statistics responses and requires mapping response/
shard metadata, explicit integer zero failed shards and mapping indices. Missing
failure counts no longer imply success. Per-index usage remains informational
and absent usage stays supported. Existing missing-active-index fixture aligned
with a complete stats response. No tests added or run under the recorded
constraint; diff whitespace checks passed. No live cleanup/services/settings/
corpus/cache changes or new metrics claimed. See index-generation-retention.

## Latest milestone: retention alias metadata validation (2026-10-04)

Generation inventory unwraps index metadata and requires mapping top-level/index
entries plus mapping alias collections/details with nonempty string names.
Malformed empty alias lists cannot imply an unaliased cleanup candidate; invalid
metadata blocks inspection before eligibility. Filtered omitted alias fields
remain supported, and reported aliases retain protection. No tests added or run
under the recorded constraint; diff whitespace checks passed. No live cleanup,
services/settings/corpus/cache changes or new metrics claimed.

## Latest milestone: explicit reviewed retention policy (2026-10-04)

Cleanup plans require exactly min_age_hours and keep_retired policy fields;
missing values no longer silently use defaults during apply. Generated plans
already provide both fields and normal inspection defaults remain unchanged.
Invalid policies stop before maintenance locking/inventory/deletion. No tests
added or run under the recorded constraint; diff whitespace checks passed. No
live cleanup/services/settings/corpus/cache changes or new metrics claimed.

## Latest milestone: unambiguous cleanup-plan JSON (2026-10-04)

The generation-management apply CLI rejects duplicate JSON fields at every
object level rather than silently choosing the last value. Invalid plans stop
before apply/inventory/maintenance locking with a fixed diagnostic that does not
echo field contents. Generated plans remain unchanged; existing lifecycle/UUID
retry checks reviewed without changes. No tests added or run under the recorded
constraint; diff whitespace checks passed. No live cleanup/services/settings/
corpus/cache changes or new metrics claimed.

## Latest milestone: retrieval completion metadata types (2026-10-04)

Retrieval unwraps Elasticsearch bodies and validates mapping responses plus
reported timeout/shard completion types. Falsey nonboolean timeouts and invalid
shard failure counts raise existing InvalidSearchResponseError; true timeouts/
positive failures retain IncompleteSearchError and sanitized 503/no-fill behavior.
Omitted completion metadata retains prior compatibility; no new omitted-field
completeness guarantee. Pending sync replay ordering reviewed without changes.
No tests added or run under the recorded constraint; diff whitespace checks
passed. No live evaluation/services/settings/corpus/cache changes or new metrics.

## Latest milestone: duplicate cached JSON field rejection (2026-10-04)

Cache decoding rejects duplicate object fields at every nesting level instead
of silently selecting the last value. Corrupt entries become generation-bound
misses and retain healthy conditional refill; normal serialized writes remain
unchanged. No full row schema validation, key migration, rotation or flush added.
No tests added or run under the recorded constraint; diff whitespace checks
passed. No live Redis/services/settings/corpus changes or new metrics claimed.

See cache-consistency.

## Latest milestone: Redis generation reply validation (2026-10-04)

Cache reads require two-item list/tuple Lua replies with nonempty string
generation tokens. Invalid replies return unknown-generation misses, keeping
retrieval independent and disabling cache fill/coalescing. Cache writes reject
invalid/empty tokens before Redis access. Valid opaque tokens and corrupt-JSON
generation-bound refill remain supported; decoded Redis configuration unchanged.
No tests added or run under the recorded constraint; diff whitespace checks
passed. No live Redis/services/settings/corpus changes or new metrics claimed.

## Latest milestone: literal cache-cleanup generation matching (2026-10-04)

Best-effort invalidation cleanup escapes Redis glob metacharacters in old opaque
generation tokens before scanning. Tokens cannot broaden physical cleanup to
other generations. Missing/invalid old tokens skip cleanup after successful
rotation; strict rotation/fencing and normal random tokens remain unchanged.
No tests added or run under the recorded constraint; diff whitespace checks
passed. No live Redis/services/settings/corpus changes or new metrics claimed.

## Latest milestone: webhook decoder recursion fallback (2026-10-04)

Authenticated webhook JSON decoder RecursionError now returns existing sanitized
HTTP 400 Invalid webhook JSON before database lookup/background scheduling,
instead of escaping as a server error. Signature verification remains first;
valid payload behavior and existing body limit unchanged. Coalescing failure/
timeout lifecycle reviewed without changes. No tests added or run under the
recorded constraint; diff whitespace checks passed. No live services/settings/
corpus/cache changes or new metrics claimed. See webhooks.

## Latest milestone: duplicate webhook JSON field rejection (2026-10-04)

Signed webhook payloads reject duplicate object fields at all nesting levels
with existing sanitized HTTP 400 Invalid webhook JSON. Ambiguous repository/
clone_url values cannot silently choose the last field for scheduling. Signature
verification remains before parsing; rejected payloads perform no database
lookup/background work. Body middleware reviewed without changes. No tests added
or run under the recorded constraint; diff whitespace checks passed. No live
services/settings/corpus/cache changes or new metrics claimed. See webhooks.
