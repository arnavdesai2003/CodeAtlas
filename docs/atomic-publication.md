# Atomic full-index publication

Full Elasticsearch indexing now rebuilds a repository in a separate **whole
corpus** index. It copies the published documents for all other repositories,
adds the target repository's committed symbols, validates document counts and
refreshes the staging index before switching `codeatlas_symbols_active`.
Staging preserves the source's effective mappings and its analysis, similarity,
vector-source, shard/replica and refresh settings instead of relying on changing
Elasticsearch defaults. The legacy physical index `codeatlas_symbols` is retained. Before the first
publication, readers and incremental writers use that physical index; the
first publication adds the new alias without deleting or renaming it.

The switch uses one [Elasticsearch aliases API request](https://www.elastic.co/docs/reference/elasticsearch/aliases).
Subsequent switches remove the alias from its exact previous target with
`must_exist=true` and add the validated stage in the same request. Response
errors/unacknowledged outcomes are failures that require outcome inspection
on retry. Searches resolve the alias once per hybrid call and pass the same
concrete index to both concurrent retrieval branches. Retired indices stay
available so an in-flight request can finish on its pinned generation.

## Coordination and recovery

`index_publication_jobs` is an additive PostgreSQL singleton table created by
startup. The journal records repository ID, source/staging index names,
`building`/`ready`/`published` phase and validation statistics. The existing
per-repository full-index job remains until finalization.

Normal sync/symbol writers acquire a shared corpus advisory lock followed by
an exclusive repository lock. Full Elasticsearch publication acquires an
exclusive corpus lock followed by its repository lock. Session locks survive
metadata commits. Different normal repository writers may proceed concurrently;
a full publication excludes all of them because it copies the entire index.
Lock contention returns HTTP 409 through the existing sync exception handler.

A pending singleton journal blocks **all cooperating repository writers** after
a publisher crashes, even though its advisory locks have been released. Only
full Elasticsearch publication for that recorded repository can resume. This
prevents later writers from changing the source or target snapshot before retry.

1. Commit the journal and, if needed, the repository full-index job before
   staging writes. Initial provisioning may create an empty legacy index.
2. Commit a fresh random staging name and lifecycle record for each build attempt.
   Capture/commit its physical UUID after creation and before copying. Copy every
   document except those belonging to the target repository. Embed/index the
   target's committed symbols, refresh, and compare copied/staged counts.
3. Commit `ready` and validation statistics before the alias request. Revalidate
   the stage count before a resumed switch. An unexpected active index or
   altered ready stage fails with an operator reconciliation error. Recorded
   source/stage UUIDs are checked before switching.
4. Switch the alias. If its acknowledgement or the next metadata commit is
   lost, retry inspects the alias: an already-active stage is never rebuilt.
5. Commit `published` with generation publication/retirement history, rotate Redis
   generation in strict mode, then delete both
   jobs together. Redis/final-commit failures retain recovery work. A repeated
   rotation fences stale fills again. Search availability does not require that
   final database transaction to have completed.

A timed-out Elasticsearch request can continue running after the caller loses
its response. Consequently a `building` retry uses a **new** staging index;
it does not clear/reuse the ambiguous old one. Abandoned stages are retained,
just like retired published generations. No automatic generation deletion is
implemented. A read-only inventory and reviewed maintenance-only cleanup command
are available in [generation retention](index-generation-retention.md). Unknown
history remains protected; readers must be quiescent before applying cleanup.

## Deployment and commands

Upgrade/restart **all** API and writer processes together before publication.
Old readers keep using the legacy index and old writers do not honor the new
corpus lock/journal. Startup creates the additive table; no existing table
columns change. There is no automatic live corpus rebuild just to install this
code. Normal full-index commands perform migration on their first publication.

Inspect outstanding publication before retrying:

```sql
SELECT repository_id, source_index, staging_index, phase, stats
FROM index_publication_jobs;
```

After resolving a dependency failure, resume the recorded repository directly:

```sh
.venv/bin/python -m scripts.index_elasticsearch --repository-id 3
```

The argument defaults to repository ID 1 for compatibility. The batch command
still works, but earlier non-owning repositories can fail while a pending owner
later in the batch is being resumed; its nonzero exit status must be respected.
Do not delete journals, edit generated clones, remove the active alias or
manually switch it during pending work. After migration, deleting the alias
would cause legacy fallback and serve the retained old corpus. A rollback also
needs coordinated metadata/cache handling; retained data alone is not an
automatic rollback protocol. Use the reviewed
[retention workflow](index-generation-retention.md); legacy/active/journaled
indices are protected.

## Scope and limits

Atomicity covers a **single full repository rebuild's Elasticsearch publication**.
It does not turn an entire batch into one snapshot or make PostgreSQL, Redis
and Elasticsearch one transaction. Symbol metadata commits before publication.
Cached old results can remain visible between the alias switch and successful
Redis rotation (longer if Redis is down). An already-running caller may return
its old result; generation fencing rejects its later cache fill after rotation.
See [cache consistency](cache-consistency.md).

Incremental sync continues to update the resolved current index in place. It
participates in corpus coordination but can still expose a partial incremental
update. No immutable snapshot guarantee is claimed during incremental sync.
Direct helper calls, legacy/external writers, a broken advisory-lock connection
or manual alias changes are outside the cooperating-writer guarantee.

Effective source mappings, model/dimensions, ranking weights and candidate counts
are preserved.
Copying vectors uses Elasticsearch's documented
[vector rehydration during reindex](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/dense-vector).
Rebuilding approximate vector structures can change tied/approximate retrieval
ordering; the quality gate remains mandatory for a real corpus rebuild.
The workflow copies the whole corpus for every repository publication and
serializes cooperating writers while doing so. It adds one alias lookup per
hybrid engine invocation; warm cache hits bypass the engine. See
[measured comparisons](performance.md).

## Verification (2026-10-01)

- 96 offline tests passed, including deterministic alias changes between
  branches, journal/ready/final commit failures, ambiguous alias outcomes,
  fresh-name build retries, count validation, pending-job exclusion and
  shared/exclusive lock behavior. Tests use SQLite metadata and mocked services.
- Live PostgreSQL verified additive table creation, shared locks for different
  repositories, exclusive publication exclusion and persistence across commits.
  No live repository metadata was changed.
- Isolated real Elasticsearch/Redis tests verified retained source data during
  build, first alias migration, subsequent alias switches, repository isolation,
  stale-symbol removal, vector search after copying, empty replacement,
  invalidation failure recovery, stale-fill rejection and lost-ack recovery.
  Metadata used temporary SQLite and embeddings used deterministic vectors.
  Temporary indices/aliases and Redis keys were removed after testing.
- On the unchanged existing 4,340-document corpus, all 25 evaluation cases were
  valid and every baseline metric matched, including hybrid Recall@10 0.880
  and MRR 0.499. No live corpus publication was performed merely for validation;
  this is not a quality measurement of a newly rebuilt production generation.
- A real-corpus scratch publication rebuilt micrograd's 40 symbols with actual
  CPU MiniLM embeddings and copied 4,300 other documents. PostgreSQL was read
  only to populate temporary SQLite metadata; cache invalidation was stubbed
  for this separate quality experiment. The 4,340-document published scratch
  generation reproduced every BM25, hybrid and reranked metric; hybrid Recall@10
  remained 0.880 and MRR 0.499. Standalone semantic Recall@10 was 0.840 and MRR
  0.581, below the live baseline 0.880/0.585. An initial scratch copy itself had
  semantic Recall@10 0.800/MRR 0.561 before publication. The effective live and
  new vector mappings were identical (`bbq_hnsw`); approximate vector rebuilds
  showed quality sensitivity without a ranking change. No exact semantic
  reproducibility is claimed. All scratch indices/aliases were removed. See
  [performance and quality results](performance.md) for the full comparison.
- Dedicated loopback port 8001 was used for before/after uncached HTTP runs;
  normal port 8000 was not restarted. No Redis flush or default device/thread
  change was made. The temporary benchmark server is stopped after verification.

### Database acknowledgement loss

A database error can follow a successful commit. Fresh-session offline tests
cover committed `ready` and `published` records whose acknowledgements are lost.
A ready retry revalidates and switches the same stage without copying/indexing
again. A published retry preserves the first publication timestamp, performs
strict cache rotation and removes the journals without another alias switch.

If the final journal-removal commit succeeded before acknowledgement loss, the
active alias and published lifecycle record remain, and both publication/full
journals are absent. Inspect durable state before treating this as pending work.
Another full Elasticsearch indexing command with no journal starts a new rebuild;
it cannot reconstruct the lost response as an idempotent retry. Do not manually
rewind aliases or recreate journals. Tests verify SQLite application transaction
logic with mocked Elasticsearch/Redis, not PostgreSQL network failure.

### Refresh failures

Both repository bulk refresh and final staging refresh now require an explicit
integer `_shards.failed` of zero. Failed or unverifiable refresh responses stop
publication before the alias switch and cache rotation. The job remains building;
retry uses a fresh staging name under the existing recovery policy. Successful
bulk counts and staging document counts alone do not bypass this check.

The Elasticsearch [refresh response](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-indices-refresh)
reports failed shard operations. Offline tests inject failures and verify journal,
alias and cache behavior. An isolated real one-document index verified the
successful-response path and was removed afterward; no live shard outage was
induced.

### Staging creation acknowledgements

Staging creation requires both `acknowledged` and `shards_acknowledged` to be
explicitly true before copying or indexing documents. False, missing or malformed
acknowledgements stop the build with its journal intact. The Elasticsearch
[create index API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-indices-create)
notes that creation may still succeed after an unacknowledged response. The
application therefore retains the potentially created stage and retries with a
fresh random name instead of reusing or deleting it.

The abandoned attempt has no recorded UUID if acknowledgement failed before
identity recording. Existing retention rules protect such unverified attempts;
this guard does not add automatic reconciliation or cleanup. Legacy index
provisioning is unchanged. Offline tests simulate creation taking effect before
returning an unacknowledged response and verify no copy, bulk work, alias switch
or cache rotation occurs before retry. An isolated real staging-creation check
verified the successful response and copied mapping; both scratch indices were
removed afterward.

### Complete publication counts

Source-copy expectations, the built stage's document total and ready-stage retry
validation all use checked counts. Responses must report integer `_shards.failed`
zero and a nonnegative integer `count`; missing/invalid fields or a timeout stop
publication. A numerically matching partial count cannot authorize an alias
switch. Elasticsearch documents shard failures in its
[Count API response](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-count).

A source/build-time count failure leaves building work for a fresh-stage retry.
A ready-stage count failure preserves the prepared stage and retries validation
without copying or indexing again. Existing writer exclusion, identity checks
and cache finalization remain unchanged. This is a count/completeness check,
not a checksum of document content.

Offline tests cover partial source and ready counts, malformed values and
successful recovery. Real isolated checks verified empty, populated and filtered
counts; the scratch index was removed. No live shard outage was induced.

Staging-copy responses also require explicit `timed_out=false`, an empty failure
list and integer zero version conflicts. Both `total` and `created` must be
nonnegative integers matching the checked source count; booleans and floats are
rejected even when numerically equal. Missing/malformed completion fields stop
before target indexing or alias publication, leaving the building journal for a
fresh-stage retry. Existing success/count-mismatch fixtures were aligned with
this response contract; no tests were added or run for this follow-up.

Alias-switch acknowledgement requires a mapping with literal
`acknowledged=true`. The optional `errors` field must be literal `false` when
present; older successful responses without that field remain accepted.
Malformed or unacknowledged responses stop finalization and retain the ready
journal. Because the switch may already have happened, retry inspects the actual
alias target before deciding whether to switch or finalize; it never rebuilds an
already active stage. No tests were added or run for this acknowledgement change.

Count responses and their `_shards` fields must be mappings. The optional
`timed_out` field must be literal `false` when present; normal Count responses
without it remain supported. Malformed containers or falsey nonboolean timeout
values raise the publication count error rather than authorizing a switch or
raising incidental attribute errors. Staging-creation acknowledgement responses
also require a mapping before checking both boolean acknowledgements. These
guards preserve the existing building/ready retry paths. No tests were added or
run for this follow-up.

The shared refresh validator requires mapping responses and shard metadata
before checking integer zero shard failures. Malformed containers produce the
existing incomplete-refresh error, retaining building work and stopping alias
publication. No tests were added or run for this response-shape guard.

Alias resolution requires a single-target mapping with a nonempty string index
name and mapping metadata containing the requested alias. Malformed or unrelated
responses raise instead of being used as a concrete generation or falling back
to legacy routing. Only an Elasticsearch not-found response enables the existing
legacy fallback. Resolution still occurs once per hybrid request, with the same
target shared by both branches. Existing success fixtures were aligned; no tests
were added or run for this follow-up.

The installed Elasticsearch client returns `ObjectApiResponse` objects that
delegate dictionary access but do not implement `Mapping`. Alias, count,
creation, reindex and alias-switch validators unwrap these objects to their JSON
body before checking structure. Plain dictionary responses remain supported;
malformed wrapped bodies still fail the same checks. This corrects the earlier
container guards, which otherwise reject valid client responses. The shared
refresh and incremental deletion validators use the same unwrapping. No tests
were added or run for this compatibility correction.
