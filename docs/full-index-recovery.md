# Coordinated full-index recovery

Full symbol and Elasticsearch indexers now share the PostgreSQL session advisory
lock used by incremental sync. It survives metadata commits. Cooperating writers
for one repository are excluded while another owns the lock; different
repositories can proceed independently. Batch scripts report failures with a
nonzero exit status and continue processing other repositories.

## Durable workflow

Startup `init_db()` creates the additive `repository_full_index_jobs` table.
Upgrade all writer processes together and restart the API before using the new
indexers. Legacy processes do not honor these guards.

1. Symbol indexing checks for pending sync work before changing metadata.
2. It replaces the repository's symbol snapshot and commits a full-index job in
   the same transaction. Symbols for missing or non-Python files are removed.
   Parsing/transaction failures roll back the snapshot and job together.
3. A repeated symbol-index command returns the recorded preparation statistics
   with `resumed=true`, without reading the checkout or replacing committed IDs.
4. Full Elasticsearch indexing deletes **all** documents for that repository,
   then embeds and indexes the committed symbol snapshot. This removes documents
   whose old symbol IDs or paths are no longer in PostgreSQL, including when the
   replacement snapshot is empty. Partial delete/bulk failures are errors.
5. Strict Redis generation rotation must succeed before the job is removed.
   A final commit failure leaves the job available for replay. Physical cache
   cleanup remains best-effort under the existing generation protocol.

Standalone Elasticsearch indexing also creates a durable job before any external
writes. While such a job is pending, symbol replacement is rejected. Full
indexing rejects pending sync jobs; sync rejects pending full-index jobs before
fetching or resetting the checkout. Neither path silently adopts the other's
work. Full rebuilding does not advance the Git checkpoint: it rebuilds the
registered file catalog, rather than performing Git discovery/synchronization.

## Recover after a failure

Resolve the dependency failure and rerun:

```sh
.venv/bin/python -m scripts.index_all_elasticsearch
```

The single-repository Elasticsearch script still targets repository ID 1.
To inspect outstanding work without changing it:

```sql
SELECT repository_id, stats FROM repository_full_index_jobs;
```

Do not delete pending jobs or edit generated clones. If full symbol preparation
failed before commit, rerun that step first. If it committed successfully,
finish Elasticsearch publication before syncing newer Git changes.

## Limits and validation

This milestone coordinates and recovers **in-place publication**. Searches may
observe missing or mixed documents while deletion/bulk indexing runs; cached
results can remain visible until the final generation rotation. The entire
multi-repository batch is not one transaction. Atomic publication still needs a
staging-index and publication protocol, including migration of the current
physical index and consistent handling of the two parallel retrieval branches.
A broken advisory-lock connection, legacy process or external index writer is
not fenced by Elasticsearch. PostgreSQL and Elasticsearch are not cross-store
atomic. An ambiguous final-commit acknowledgement may mean the job is already
removed; retrying then performs another safe rebuild.

Offline tests use real temporary SQLite transactions and mocked external
services. They exercise committed snapshot replay in a fresh session, parsing
rollback, missing-file removal, lock acquisition before preflight, pending-job
exclusion, incomplete deletion, indexing failure, cache rotation failure and
final-commit failure. No live corpus rebuild is needed to validate these faults.

## Verification on 2026-10-01

- 73 offline tests passed (61 inherited plus 12 new recovery cases).
- Live PostgreSQL created the additive table and verified shared-lock exclusion
  across commit and release after both success and failure. No corpus rows changed.
- A temporary Elasticsearch index and isolated Redis keys verified full deletion,
  removal of stale IDs, preservation of another repository's documents, retry
  after an injected invalidation failure, generation rotation, and an empty
  replacement snapshot. PostgreSQL metadata for this smoke test was represented
  by temporary SQLite; embeddings were deterministic test vectors. The temporary
  search index and Redis keys were removed afterward.
- Multi-repository evaluation on the existing live corpus found 25 valid cases,
  zero invalid cases, and reproduced every baseline metric across all four
  methods, including hybrid Recall@10 0.880 and MRR 0.499. This evaluated the
  existing index; it does not claim a newly rebuilt live corpus.
- Docker Desktop and the existing three service containers were started for
  verification. No API server was restarted or benchmark server launched.
  No latency benchmark or performance improvement is claimed.
