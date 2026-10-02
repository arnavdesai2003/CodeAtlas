# Cache consistency

Search captures the current Redis generation before retrieving. A Lua read
returns both generation and cached value in one operation. A Lua fill compares
that original token with the active generation and writes with the configured
TTL only if they match. Query normalization and limit semantics are unchanged.

Invalidation atomically replaces the generation with a random UUID, then scans
and deletes only the previous generation in batches of 256. Correctness does
not depend on deletion: remaining old entries expire and cannot be read under
the new generation. Strict invalidation raises on rotation failure so durable
sync can retry. Cleanup errors after successful rotation are tolerated. The
returned count, including sync's `cache_entries_invalidated`, is physical
deletions and can be zero despite successful logical invalidation.

The generation key has no TTL. If it is evicted or Redis resets, the next read
creates a new random token; old entries cannot become active again. If the
initial lookup fails, retrieval proceeds without a cache fill: obtaining a new
token after retrieval would reintroduce the stale-fill race. Cache writes remain
best-effort, and benchmark bypass skips all Redis reads and writes.

## Scope and deployment

Keys use `codeatlas:search:v2:<generation>:<query digest>`; generation metadata
uses `codeatlas:search:generation:v2`. Legacy entries expire naturally. Upgrade
all API, indexing and sync processes together, since older processes do not
honor generation rotation. Existing port 8000 processes are not automatically
restarted by development benchmarks. No Redis flush is required.

These Lua operations target the existing standalone Redis deployment and require
script execution permissions; they are not a Redis Cluster implementation.
This prevents stale cache fills after successful invalidation. It does not
cancel outstanding responses, make partial incremental Elasticsearch updates
invisible. A separate process-local layer now coalesces eligible simultaneous
misses by exact query/limit/generation; see [cache coalescing](cache-coalescing.md).
Full rebuilds now publish a staged
index atomically; old cache hits remain possible until the subsequent rotation.
See [atomic publication](atomic-publication.md). An already-running request may return old data,
but cannot seed that data into the new cache generation.

## Verification

The regression test invalidates inside a mocked retrieval, returns the old
result, then confirms the next request retrieves fresh data and the following
one hits it. It failed before this change. Other offline cases cover late
writers, generation eviction, cleanup failures, malformed entries and outages.
At that generation-fencing checkpoint, all 61 tests passed. An isolated live Redis namespace verified actual Lua reads,
conditional fills, TTL, invalidation and metadata eviction; temporary keys were
removed afterward. See [performance results](performance.md) for HTTP checks.

## Cache value bounds (2026-10-02)

Reads reject result lists exceeding the requested limit, non-dictionary rows,
non-finite JSON constants (NaN/Infinity) and floating-point overflow such as 1e400,
including nested values. Rejection becomes a miss bound to the original read
generation, allowing ordinary retrieval/conditional replacement without rebinding
to a newer generation. Valid empty lists and finite numeric values remain hits;
words like `NaN` inside source strings remain unchanged.

Writes reject invalid list shape/row count and serialize with allow_nan=False;
failed serialization returns false without invoking the Redis Lua write. It does
not block the caller's retrieved result. No Redis flush, generation rotation,
entry schema migration or per-symbol field validation was added. This is a cache
boundary, not fresh engine-output validation or a complete result schema.

219 offline tests pass, covering numeric constants/overflow, nested values,
over-limit lists, no-write failures and valid finite/empty hits alongside fencing
and outage cases. An isolated CPU/one-thread, one-worker HTTP smoke had 80
successful requests, including six Redis hits, verified payload equality and
engine attribution. Server stopped/private namespace removed; legacy routing,
index UUID and 4,340 documents unchanged. No live generation/settings/process
changes. Retrieval evaluation remains inherited; no latency/throughput claim.

## Excessively nested entries

Cache result validation now limits list/dictionary nesting to 16 container levels,
counting the outer results list. Normal search rows contain scalar fields and
remain unchanged. Validation uses an explicit stack; excessive or cyclic nested
write values are rejected before contacting Redis. JSON decoder recursion errors
also become cache misses rather than escaping into the request.

An invalid read preserves its observed generation token, allowing retrieval to
conditionally replace the entry under the existing fence. This introduces no
key migration, generation rotation or Redis flush. The check is not a full row
schema validator or a cache payload byte-size limit. Existing valid cache hits,
empty results and bypass behavior stay unchanged.

Offline tests reproduce excessive nesting, verify decoder-error fallback,
reject cyclic/deep writes and exercise healthy miss/refill behavior with clean
coalescing state. Isolated real Redis verified nested corruption, retained
generation, healthy refill and TTL; both private keys were removed. Retrieval
and ranking were unchanged; no new evaluation or throughput claim was made.
