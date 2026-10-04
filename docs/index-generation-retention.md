# Index generation inspection and retention

Full publications retain old source indices and ambiguous build attempts. The
operator command reports routing, physical index UUIDs, document/storage usage,
lifecycle history and pending recovery jobs. Inspection and planning are read
only. Nothing automatically deletes generations, aliases, journals or Redis keys.

```sh
.venv/bin/python -m scripts.manage_index_generations inspect
.venv/bin/python -m scripts.manage_index_generations plan \
  --output /private/tmp/codeatlas-generation-plan.json
```

The plan file must be new; existing reviewed files are never overwritten. Review
its explicit candidate names, UUIDs, states, inactive times, cluster identity and
retention policy. Inspection explains each protected index and supplies the
publication owner's resume command when recovery is pending. An empty plan is
normal before tracked generations have accumulated.

## Lifecycle and eligibility

`search_index_generations` is an additive PostgreSQL audit table created by
`init_db()` at startup. Upgrade all API and writer processes together before
using the protocol. No existing table columns change, no corpus rebuild is
required, and the command fails if the schema/services are unavailable.

The journaled build attempt gets a `building` audit record before index creation;
its Elasticsearch UUID is committed before copying/indexing. A failed build's
next attempt marks the tracked previous attempt `abandoned` and records its
inactive time. If the creation acknowledgement or identity commit was lost,
that attempt has an unverified identity and stays protected. Older untracked
attempts also stay protected. The tool never guesses their history from an
index name or Elasticsearch creation date.

Confirmed publication records the new generation as `published`, and the
previous generated source as `retired` with its confirmation time. A retry after
Redis/final-commit failure preserves this first retirement time. A previous
version's source/ready stage can be adopted when confirmed by publication; this
conservatively starts the source's grace period at confirmation. Replaced
physical identities fail publication rather than being silently adopted. Audit
rows survive successful job finalization and later index deletion.

Defaults require **24 hours inactive** and retain the **two newest physically
present retired published generations**, in addition to all other protections.
`--min-age-hours` and `--keep-retired` can change the plan's policy; the minimum
values are one hour and one retained generation. Abandoned attempts do not
consume the retained published-generation allowance. Grace starts at retirement
or abandonment, never at creation: a years-old index retired just now remains
protected. These retained copies support diagnosis; they do not provide a
coordinated PostgreSQL/Elasticsearch/Redis rollback protocol.

An index can be a candidate only if it has an exact generated name, recorded
matching physical UUID, `retired` or `abandoned` state and a sufficiently old
inactive timestamp. The following stay protected:

- The legacy physical index, resolved active target and every index with any alias.
- The publication journal's source/stage, untracked or malformed names, and
  building/published/deleted or otherwise unexpected lifecycle states.
- Unverified/mismatched identities, unknown or recent inactive times, and the
  newest retired generations required by policy.

Missing indices remain visible in the audit inventory. They are not new deletion
candidates. Missing active routing, failed statistics or unavailable dependencies
block application. Unknown history needs separate investigation; there is no
force-delete or automatic backfill option.

## Apply during maintenance

Readers pin physical index names but have no leases or maximum execution time.
A grace period alone cannot prove that an old index is unused. **Stop all API,
search/benchmark readers and repository/indexing writers before applying a
reviewed plan.** Resolve pending sync/full/publication jobs first. Wait for
outstanding Elasticsearch writes, including a reindex request that outlived its
publisher, to finish. Keep those processes stopped until cleanup exits.

```sh
.venv/bin/python -m scripts.manage_index_generations apply \
  --plan /private/tmp/codeatlas-generation-plan.json --quiesced
```

`--quiesced` is the operator's explicit attestation, not a process detector.
The command itself holds the exclusive PostgreSQL corpus advisory lock across
audit commits, rejects pending jobs, and checks Elasticsearch write tasks before
deleting. Its task inspection uses the Elasticsearch tasks API (technical
preview in the tested client). Incomplete/failed task inspection blocks cleanup.
Locks coordinate current CodeAtlas writers; they do not stop old/external
processes, direct helper calls or readers. Manual alias/index changes during
maintenance remain outside the guarantee. Do not use this command against an
actively serving namespace merely because its lock can be acquired.

Apply checks cluster identity and revalidates **every reviewed candidate before
the first deletion**. It then repeats inventory, candidate protection/UUID checks
and task inspection before each target. It never expands the plan to additional
indices that became eligible later. Changed UUIDs, aliases, routing or lifecycle
history require a new inspection/plan. Each deletion names one exact index;
there are no wildcard writes. The legacy index is always retained.

Each acknowledged deletion commits `deleted` and a deletion timestamp while
keeping its UUID and earlier history. Failure exits nonzero with a structured
partial result: acknowledged deletions, already-absent targets, failed index,
error type and a safe diagnostic. A timeout or database commit failure can leave
an ambiguous outcome; inspect the physical index and audit before retrying.
A repeated unchanged plan can reconcile an absent target with the same recorded
UUID/history, without issuing another deletion. It will refuse a recreated index
or changed history. There is no cross-store transaction or automatic rollback of
a partly applied plan.

## Verification (2026-10-01)

- 122 offline tests pass. Added coverage includes recent retirement of old
  indices, unknown identities/history, aliases and journal protections, pending
  jobs, stale plans, all-target prevalidation, inter-target identity changes,
  incomplete write-task inspection, lost/unacknowledged deletions, audit-commit
  failures, idempotent reconciliation and exclusive plan-file creation. Publication
  tests cover identity changes, retirement timestamps, upgrade adoption and
  failures before identity durability. Tests use SQLite and mocked services.
- Live PostgreSQL verified the additive audit table and exclusive maintenance
  lock against normal writers and publishers, persistence across commits and
  release after exceptions. No live repository/audit rows were changed.
- Isolated real Elasticsearch fixtures verified UUID/alias/statistics inventory,
  task inspection, exact reviewed deletions, retained audit, idempotent retry and
  preservation of legacy/active/aliased/untracked/newest-retired indices. Their
  readers and writers were quiescent; all fixture indices/aliases were removed.
- Isolated publication recovery was rerun against real Elasticsearch/Redis with
  temporary SQLite metadata and deterministic embeddings. Migration, subsequent
  switches, retained sources, Redis fencing and lost-ack recovery passed; all
  fixture indices/aliases/keys were removed.
- Live read-only inspection confirmed the existing legacy index is active and
  protected. No live cleanup, alias switch, corpus rebuild or API restart was
  performed. Inspection measured 4,340 live documents. CPU/one-thread evaluation
  validated all 25 cases and reproduced every baseline metric, including hybrid
  Recall@10 0.880 and MRR 0.499. This evaluates the unchanged existing index;
  it is not a new approximate-vector rebuild quality measurement.

This milestone changes publication bookkeeping and maintenance tooling, not the
request retrieval path, model, ranking or candidate sizes. It makes no new
latency or capacity claim. Existing alias-resolution performance limitations
remain in [performance](performance.md).

### Lost audit-commit acknowledgements

An acknowledged Elasticsearch deletion can be followed by a successful audit
commit whose database acknowledgement is lost. The command still reports failure
and stops before the next candidate. Inspect durable state through a fresh
session: the deleted audit row may already be committed.

An unchanged reviewed plan can reconcile that absent index, preserve its first
`deleted_at` timestamp and continue with remaining candidates. If the name has
been recreated with a different UUID, retry blocks before any further deletion,
even when the old audit row already says `deleted`. Do not edit the old audit to
match a replacement index. Existing quiescence and writer-lock requirements still
apply to retries.

Offline tests commit the audit before injecting acknowledgement loss and retry
with fresh SQLite sessions and simulated Elasticsearch identities. They do not
simulate a PostgreSQL network fault or change retention policy.

Deletion acknowledgement is checked against the JSON body of Elasticsearch's
`ObjectApiResponse` wrapper (plain dictionaries are also supported). The body
must be a mapping with literal `acknowledged=true`; truthy strings/integers and
malformed responses cannot authorize the deleted audit state. An ambiguous
response stops before that audit update or any further candidate. Retry uses the
existing exact identity/history checks to reconcile a deletion that may already
have happened. No tests were added or run for this acknowledgement guard, and no
live cleanup was performed.

Index metadata is unwrapped and validated as a mapping rather than coerced with
`dict()`. Index entries and alias collections/details must be mappings with
nonempty string names. Malformed empty lists cannot imply an unaliased index;
invalid metadata blocks inspection before eligibility is calculated. Omitted
alias fields remain supported for filtered responses, while any reported alias
protects its index as before. No tests were added or run for this guard, and no
live cleanup was performed.

Write-task inspection unwraps client response bodies and requires a mapping
`nodes` collection with mapping node entries and task collections. Optional
`node_failures`/`task_failures` must be empty lists when present; malformed falsey
values are not evidence of successful inspection. An empty `nodes` mapping is
accepted. Malformed shapes or any reported write task block cleanup before
deletion; task API errors remain failures. This does not prove reader quiescence
or exclude external writes starting after inspection. No tests were added or run
for this guard, and no live cleanup was performed.

Inventory statistics require response/shard mappings, explicit integer
`_shards.failed=0` and an `indices` mapping after client-body unwrapping. Missing
or malformed completion metadata blocks inspection rather than defaulting to
zero failures. Per-index statistics may still be absent (for example for closed
indices); usage remains informational and eligibility still depends on lifecycle,
identity and protection checks. The existing missing-active-index fixture uses a
complete statistics response. No tests were added or run for this guard, and no
live cleanup was performed.
