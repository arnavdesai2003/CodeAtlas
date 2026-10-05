# Local developer release acceptance

Verified 2026-10-05: `scripts.verify_project --live` exited 0 and passed all 12
sequential checks, including the full 448-test offline suite. Ten new regressions
cover opt-in scope, sequencing, pending/malformed jobs, changed corpus identity,
failure sanitization, owned timeout cleanup and API private-cache cleanup. Both
new entrypoints' actual help passed. The live API miss/hit smoke passed and removed
its private keys; no table initialization or normal cache generation change.

CodeAtlas's implemented delivery scope is a local Python-symbol code-search
service: GitHub ingestion, Python extraction, hybrid retrieval, optional offline
reranking, cache fencing/coalescing, incremental synchronization, staged full
publication, recoverable writer jobs and reviewed maintenance retention. It
includes API access/validation limits, operational inspection, quality evaluation
and workload diagnostics. Public hosting, parsing other languages, distributed
coalescing, cross-store atomic snapshots, online reader leases and automatic
rollback are not implemented by this release.

## Repeatable acceptance command

From the configured Python 3.13 environment with existing corpus and healthy
PostgreSQL/Elasticsearch/Redis:

```sh
.venv/bin/python -B -m scripts.verify_project --live
```

Without `--live`, only the offline suite runs. Help and invalid arguments start
no checks. Live checks run sequentially with cached CPU/one-thread models, using
process environment overrides without changing `.env` or defaults. The command
stops on the first failed check, returns nonzero and never treats partial output
as successful release verification. Pending or malformed recovery journals block
live checks. Start/end cluster, active index UUID and document count must match.
This comparison is not an atomic snapshot or exclusion of external writers.

| Acceptance area | Check |
| --- | --- |
| Offline behavior and crash recovery | Collect only `tests/`; no manual integration-script collection or model downloads. |
| Retrieval quality | 25 validated multi-repository cases, all four methods, explicit hybrid Recall@10 minimum .880. Compare other metrics with the recorded baseline. |
| API integration | Real production ASGI routes with live stores: health, repository listing, miss→identical hit, query/limit validation and configured-key rejection. |
| Cache consistency | Real Redis Lua fencing/TTL/corrupt-entry/interleaving checks in a random private namespace. |
| Full publication recovery | Real scratch Elasticsearch/Redis with temporary SQLite metadata and deterministic vectors; migration/retry/empty replacement and cleanup. |
| Writer exclusion | Real PostgreSQL advisory overlap/exclusion, commits, exception release and owned-process normal/terminated release. |
| HTTP body receipt | Owned ephemeral real-socket server validates all eight body/deadline/connection cases and stops. |
| Operational state | Read-only start/end recovery journals and generation inventory, no pending jobs and unchanged active corpus identity/count. |

The API smoke uses in-process ASGI transport and deliberately skips lifespan:
no table initialization, listening production server or existing API restart.
Its search cache uses private keys and cleanup verifies the normal generation
did not change. It does not test ASGI over TCP; the separate HTTP receipt probe
tests real sockets for middleware. Neither check is a performance benchmark.

Each subprocess has a 300-second default budget (`--timeout`, minimum 60).
Timeout/interruption terminates the owned process group, escalating to kill
after five seconds. Forced termination may bypass a probe's finally cleanup;
on a failed probe, known scratch identifiers are retained in the report for
operator inspection. Never interpret a timeout as safe artifact removal.
Reports suppress raw subprocess error details; individual safe verifier commands
remain available for diagnosis. No live corpus rebuild, alias switch, retention
deletion, normal cache flush/rotation or infrastructure change is performed.

## Limits and research

Passing acceptance demonstrates current local behavior. It does not establish
public deployment readiness, sustained capacity, cross-store atomicity or a
unique forwarding-delay cause. Existing quality/performance results have their
own workloads and dates. Retention still requires quiescence; all writer/API
processes must upgrade together for current recovery protocols. Research
completion criteria remain in [remaining milestones](remaining-milestones.md).
