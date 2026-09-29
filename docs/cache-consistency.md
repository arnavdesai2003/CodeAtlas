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
cancel outstanding responses, make partial Elasticsearch updates invisible,
or coalesce concurrent misses. An already-running request may return old data,
but cannot seed that data into the new cache generation.

## Verification

The regression test invalidates inside a mocked retrieval, returns the old
result, then confirms the next request retrieves fresh data and the following
one hits it. It failed before this change. Other offline cases cover late
writers, generation eviction, cleanup failures, malformed entries and outages.
All 61 tests pass. An isolated live Redis namespace verified actual Lua reads,
conditional fills, TTL, invalidation and metadata eviction; temporary keys were
removed afterward. See [performance results](performance.md) for HTTP checks.
