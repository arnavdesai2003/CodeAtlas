# Simultaneous cache misses

Normal search shares in-flight work within one API process when exact query
text, limit and initial Redis generation match. One leader retrieves and
conditionally fills Redis. Followers receive independent copies. Completed work
is removed immediately; this is not a second cache. Queries are never rewritten
for retrieval. Existing Redis key normalization remains unchanged.

The leader checks Redis again after acquiring its slot, avoiding retrieval when
another completed request filled Redis after the initial miss. It can use that
hit only from the original generation and never rebinds old work to a newer
token. Readers after invalidation observe a different token and cannot join old
work. Existing Lua writes reject old fills. Already-running callers that read
the old token may receive old shared results; publication/cache visibility is
still not atomic.

## Bounds and failures

- Sharing is process-local. Different workers/hosts can each run a leader; no
  distributed lock or new infrastructure is added.
- At most 128 active query/limit/generation keys are retained. At capacity,
  new keys retrieve independently; existing keys remain joinable.
- Followers have a one-second wait budget, then retrieve independently using
  their original token. They do not cancel/remove the leader. Slow retrieval
  can intentionally lead to duplicate work.
- Leaders retain existing engine timeouts, not a new one-second deadline.
  A hung leader occupies its slot until completion; the registry cap remains.
- Leader errors, including its own `TimeoutError`, propagate to current
  followers. Failure removes the slot and allows later retries.
- Cache-write failure does not prevent sharing a successful computation.
  Completed results are not retained, so later misses can retrieve again.
- An unknown generation disables sharing and fill for that request. Benchmark
  bypass skips Redis and the registry entirely.
- The registry lock is never held during Redis, retrieval or waiting. Separate
  keys run concurrently and returned result objects are independent.

These bounds do not impose a global request/engine concurrency limit or a
response-time guarantee. The leader performs an extra Redis read on eligible
misses; Redis hits and benchmark bypass do not acquire a flight.

## API timing and attribution

`POST /search` adds `cache_coalesced` and `coalescing_wait_ms`:

| Outcome | cache_hit | cache_coalesced | elasticsearch_latency_ms |
|---|---|---|---|
| Redis hit, including leader's second read | true | false | null |
| Actual retrieval | false | false | Own engine duration |
| Shared in-flight outcome | false | true | null |
| Wait expires, independent retrieval | false | false | Own engine duration |
| Benchmark bypass | false | false | Own engine duration |

`search_latency_ms` includes this caller's cache reads, wait, retrieval when
needed and fill. `coalescing_wait_ms` includes waiting that ended in independent
fallback. Followers do not borrow the leader's engine timing or count as Redis
hits. The legacy engine field still measures the whole hybrid call when this
request executes it. The bypass benchmark rejects coalesced responses.

## Verification and operation

Offline events/barriers cover shared/empty results, errors/retry, generation
rotation, no token, bypass, distinct text/limits, saturation, timeout fallback,
late fills and independent result objects. API tests verify waiter attribution.
See [performance results](performance.md) for live checks.

Restart API processes to load the change; no cache flush is needed. Mixed
versions retain the same generation-fencing protocol, but only new processes
coalesce. Generation-retention maintenance still requires stopping readers and
writers. There is no reader lease or distributed coalescing protocol.

`scripts.benchmark_miss_burst` warms models, releases same-query callers together,
counts real engine calls and verifies result/cache equality. It uses real
services with an unpredictable isolated Redis namespace, removes only its keys
and exits nonzero on errors or failed cleanup. This is direct-service burst
timing, not HTTP throughput or sustained capacity.

```sh
EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 HF_HUB_OFFLINE=1 \
  .venv/bin/python -B -m scripts.benchmark_miss_burst --workers 20 --rounds 2
```

For HTTP mixed-query and process-boundary diagnostics:

```sh
EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=1 HF_HUB_OFFLINE=1 \
  .venv/bin/python -B -m scripts.benchmark_http_miss_burst --server-workers 2
```

This script owns a temporary loopback server on port 8001, refuses occupied
ports, warms every worker and releases 20 requests together in each burst.
Five bursts each use one, two, five or ten unchanged benchmark queries. Redis
generation rotation and engine-call counters use a random diagnostic namespace;
server shutdown and exact-key cleanup run on failure too. Port 8000 is excluded.
`--app-dir PATH` selects archived application sources for matched controls;
worker readiness verifies the loaded service path. Do not deploy the diagnostic
factory `scripts.http_miss_burst_server`; use the ordinary application server.

Responses identify their worker only in the diagnostic factory. The client
checks timing/attribution, per-query result equality, actual engine counters,
participation by every worker, and unchanged index routing/UUID/document count.
Nonzero exit invalidates the run; a final `completed` record confirms cleanup.
The extra Redis increment on every engine call and diagnostic middleware affect
timing in both controls and current runs. These are instrumented HTTP bursts,
not standard benchmark throughput or sustained capacity. Requests arriving after
another request fills Redis can be hits, even though each burst starts empty.
Worker assignment depends on client connections and OS scheduling; the reported
query/worker pair count helps explain variation. More workers can duplicate
leaders; this diagnostic does not demonstrate a need for distributed locking.
