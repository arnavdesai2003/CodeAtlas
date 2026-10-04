# Repository synchronization and recovery

## Failure reproduced and repaired

Previously sync committed `Repository.last_indexed_commit` before updating
Elasticsearch. An indexing failure left the checkpoint advanced, so retrying
reported `changed=false` and never repaired the index. Offline transaction
tests reproduced this failure before the fix.

Sync now uses the additive PostgreSQL `repository_sync_jobs` table. One row per
repository records the previous/target commit, affected paths, file IDs and
change counts. No external queue or service was added.

1. Acquire a PostgreSQL session advisory lock for the repository. A concurrent
   API request gets HTTP 409; a concurrent webhook worker logs the failure.
2. If a pending job exists, finish its recorded target before fetching another
   remote commit. Otherwise fetch/diff/parse and commit metadata and the job
   together. The repository checkpoint stays at the last completed target.
3. Delete affected Elasticsearch paths and index their committed symbol IDs.
   Partial delete responses, version conflicts, timeouts and bulk failures are
   failures, not completion.
4. Invalidate Redis search entries in strict mode. An outage keeps the job
   pending. Normal search cache reads/writes remain best-effort.
5. Commit the new checkpoint and delete the job in one transaction.

Retries repeat deletion/indexing using persisted metadata and symbol IDs.
They do not reparse or fetch a newer target first. Failures before metadata
commit roll back metadata; failures after it leave the job for retry. Failure
of the final transaction likewise leaves the old checkpoint and job.
Session advisory locks survive the intermediate commit and are shared across
API/webhook processes. A connection is discarded if unlocking fails, so a
locked connection is not deliberately returned to the connection pool.

## Installation and operation

`init_db()`/FastAPI startup creates the new table through SQLAlchemy
`create_all`; existing tables need no column changes or data migration. The
table was also created explicitly in the development database during this
milestone. Restart an existing server to load the new code. This change does
not reconstruct failed synchronization jobs from older versions.

After resolving the failed dependency, retry the normal endpoint:

```sh
curl -X POST http://127.0.0.1:8000/repositories/1/sync
```

A recovered job returns `resumed=true`, `changed=true`, and its recorded
`new_commit`. If the remote advanced meanwhile, call sync again for the later
change. There is no automatic retry scheduler. HTTP 409 means another sync
owns the repository lock; retry after it finishes. Other sync errors retain
the existing HTTP 500 behavior and leave any durable job intact.

Inspect outstanding work with read-only SQL:

```sql
SELECT repository_id, old_commit, target_commit, affected_paths, file_ids
FROM repository_sync_jobs;
```

Do not delete jobs manually to clear errors: they are recovery records. Full
symbol/Elasticsearch indexers reject repositories with pending sync jobs and now
share the same repository advisory lock. Full publication also takes an
exclusive corpus lock and its durable journal excludes all cooperating writers.
Sync rejects pending full-index jobs before Git
operations. See [atomic publication](atomic-publication.md) and
[full-index recovery](full-index-recovery.md). Upgrade all writer
processes together; older processes do not participate in this coordination.

## Ingestion cleanup

Ingestion reserves the destination directory atomically before cloning. A
losing concurrent request cannot clean up the winner's directory. Failures
before the database commit attempt roll back metadata and remove only the
reserved clone. Dot path components are rejected.

The success response is assembled before committing. No post-commit refresh
can fail and trigger deletion of a registered clone. If the commit attempt
itself fails, its outcome may be ambiguous (the acknowledgement could be lost);
the clone is preserved. Inspect `GET /repositories` or PostgreSQL before
retrying. A clone left without a row requires operator reconciliation;
ingestion does not automatically delete or adopt that directory.

## Guarantees and remaining limits

This is replayable recovery, not an atomic cross-store snapshot. Queries can
observe missing/mixed documents during a partial Elasticsearch update.
Generation-based cache invalidation now rejects fills from searches started
before invalidation; an already-running caller can still receive its old result.
See [cache consistency](cache-consistency.md). Strict invalidation requires
successful generation rotation; physical cleanup is best-effort. The response
field `cache_entries_invalidated` counts physically deleted keys, not all
logically invalidated entries, and may be zero after successful rotation.
No availability or exactly-once delivery guarantee is claimed.

The lock protects cooperating sync and full-index callers. A broken lock connection or
an external or legacy writer is not fenced out by Elasticsearch. The metadata
checkpoint retains its historical ingestion meaning until initial symbol and
Elasticsearch indexing completes. Earlier-version failures that advanced a
checkpoint without a job need explicit reconciliation.

## Validation (2026-09-28)

- All 53 offline tests pass. Recovery tests use temporary SQLite databases with foreign keys, real
  SQLAlchemy commits/rollbacks and temporary clone directories. Git,
  Elasticsearch and Redis are mocked. Faults cover preparation/final commits,
  parsing, fetching, deletion, indexing, cache invalidation and clone cleanup.
  Tests also cover durable retry in a new session with a newer remote target,
  rename/add/delete paths, reservation races, ambiguous commit acknowledgements
  and full-index pending guards.
- Live PostgreSQL smoke check verified additive table creation, lock exclusion
  across metadata commit, and lock release after success/failure. No repository
  records or indexed documents changed.
- Full retrieval evaluation: 25 valid, zero invalid cases; every BM25,
  semantic, hybrid and reranked metric matched the baseline, including hybrid
  Recall@10 **0.880** and MRR **0.499**.
- No latency optimization or throughput claim in this milestone. The live
  corpus was not synchronized or reindexed merely to test recovery.

## Incremental bulk completeness (2026-10-02)

Incremental Elasticsearch indexing now checks the bulk helper's returned success
count and error collection, as full indexing already does. Any errors or a count
different from the prepared action count raise before refresh/cache invalidation/
checkpoint finalization. Existing bulk exceptions still propagate. This is a
defensive check; no live silent partial write was observed, and the helper's
existing default behavior also raises on ordinary item failures.

The existing pending sync journal retains committed file/symbol IDs after these
failures. Retry deletes affected paths again and reindexes those same stored
symbols; it does not reparse or advance to a newer target until publication ends.
An empty symbol snapshot still follows the existing deletion/invalidation path.
No cross-store atomicity or new indexing infrastructure was added.

213 offline tests pass. New SQLite/mock-store coverage injects short counts,
returned error items and excess counts, verifies retained journal/IDs/checkpoint,
no refresh/cache invalidation, then successful replay with one strict invalidation.
Live CPU/one-thread evaluation validated 25 cases and reproduced every baseline
metric, hybrid Recall@10 .880 / MRR .499. Live corpus was not synchronized or
reindexed; no data/cache/settings/process changes or performance claim.

### Lost commit acknowledgements

An error response does not prove that a database commit failed. If the metadata
preparation commit succeeds but its acknowledgement is lost, the pending sync
job and replacement symbols remain durable. Retry in a fresh session resumes
that recorded target using its existing symbol IDs, even if local source or the
remote has since advanced. Publication did not start before the failed
acknowledgement returned.

If the final checkpoint/job-deletion commit succeeds but its acknowledgement is
lost, a fresh retry sees the advanced checkpoint and no pending job. With an
unchanged remote it reports `changed=false` without repeating publication or
cache invalidation; a newer remote is handled as subsequent work. Never manually
remove a journal or rewind the checkpoint merely because a caller saw an error.

Offline transaction tests commit successfully before injecting acknowledgement
loss at each boundary, then inspect and retry through a new SQLite session.
These verify application recovery logic, not a simulated PostgreSQL network
failure or distributed atomicity.

### Refresh acknowledgement checks

After a successful incremental bulk write, the explicit Elasticsearch refresh
must report an integer `_shards.failed` of zero. Shard failures or missing/invalid
failure counts raise before strict cache invalidation and checkpoint advancement.
The committed sync journal and symbol IDs remain replayable. A retry repeats the
recorded publication work and validates refresh again; no reparse is required.
This closes a previously unchecked refresh response, not cross-store atomicity.

Incremental path deletion now requires a complete Delete By Query response:
explicit `timed_out=false`, empty failure list, integer zero version conflicts,
and matching nonnegative integer `total` and `deleted` counts. Missing or
malformed fields fail closed and leave the committed sync journal/checkpoint
retryable. Retrying repeats the idempotent path deletion before reindexing the
same committed symbol IDs. This validates response completeness; it does not
provide cross-store atomicity.

Refresh responses and their `_shards` metadata must also be mappings. Malformed
containers raise the existing incomplete-refresh error before cache invalidation
or checkpoint advancement, retaining the committed sync job for retry. This
response-shape guard also applies to full-publication refreshes. No tests were
added or run for this follow-up.

When a writer provisions a missing symbol index, creation must return a mapping
with literal `acknowledged=true` and `shards_acknowledged=true` before writes
continue. Missing, malformed or unacknowledged responses raise for retry; the
possibly created index is preserved. Retry follows the existing index-existence
check rather than deleting/recreating an ambiguous outcome. This also applies
to initial legacy provisioning during full publication; it does not validate an
existing index's mapping or add a new reconciliation protocol. No tests were
added or run for this acknowledgement guard.

The provisioning helper creates only the legacy bootstrap index
`codeatlas_symbols`. If incremental deletion/indexing resolves a missing
published generation, it raises before issuing those writes rather than creating
an empty replacement with the same name. The committed sync journal remains for
recovery; reconcile the missing generation before retrying. Existing generations
and legacy bootstrap behavior remain unchanged. This existence check does not
prevent external deletion racing a subsequent write or Elasticsearch automatic
index creation; keep direct/manual writers and deletion outside normal operation.
No tests were added or run for this guard.

Elasticsearch `ObjectApiResponse` wrappers are unwrapped to their JSON body
before validating delete, refresh and creation responses. The installed client
wrapper delegates dictionary methods without implementing `Mapping`; checking
the wrapper directly would reject valid responses. Plain dictionaries and all
strict completion/acknowledgement checks remain supported. No tests were added
or run for this compatibility correction.
