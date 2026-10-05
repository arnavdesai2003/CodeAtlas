# Verification after Docker restart — 2026-10-05

The user restarted Docker. PostgreSQL, Elasticsearch and Redis reported healthy.
Read-only retrieval runs used CPU/one-thread settings and offline Hugging Face/
Transformers flags in the process only; cached models, no downloads or defaults
changed. This checkpoint verifies live correctness, not latency/capacity.

## Retrieval

The explicit multi-repository `.880` quality gate exited 0: 25 defined/valid cases,
zero invalid cases. Every baseline metric reproduced:

| Metric | BM25 | Semantic | Hybrid | Reranked |
| --- | ---: | ---: | ---: | ---: |
| Recall@1 | .080 | .440 | .360 | .400 |
| Recall@3 | .240 | .720 | .560 | .520 |
| Recall@5 | .280 | .760 | .760 | .600 |
| Recall@10 | .280 | .880 | .880 | .800 |
| MRR | .168 | .585 | .499 | .493 |

The hardened legacy evaluator and weight sweep both exited 0 with eight valid
micrograd cases. Legacy BM25/semantic/hybrid Recall@10 was .625/.875/1.000;
MRR .358/.573/.609. Its sweep printed .60 as its recommendation; nothing was
applied. These older cases are a different evaluation set and are not the
multi-repository baseline or evidence for changing weights.

## Real protocol and failure verification

- Redis: Lua fill/read/TTL, corrupt-entry refill, stale fill rejection, literal
  glob cleanup, post-rotation refill and empty-token recovery passed. Private
  keys removed; normal cache generation unchanged.
- Publication: scratch alias migration, stale IDs, unaffected/vector copies,
  cache fencing, lost alias acknowledgement, cache-failure retry, empty snapshot,
  incremental delete/bulk/refresh passed. Temporary SQLite metadata and
  deterministic vectors were used. Scratch indices/aliases/keys removed;
  normal routing/UUID/count/cache generation unchanged.
- PostgreSQL: writer/publisher/maintenance exclusion across commit and release,
  exception release, and normal/terminated owner-process release passed. No
  metadata/schema writes; sessions/owned processes cleaned up.
- HTTP: all eight real-socket body-limit cases passed. Incomplete requests got
  408, oversized requests 413 without dispatch, exact-size requests 200 and
  accepted connection reuse worked. Ephemeral loopback probe server stopped.

The HTTP verifier was the remaining entrypoint ignoring arguments. It now
parses before socket/server work and returns sanitized nonzero probe/cleanup
failures. Three offline command tests and actual help passed.
All 434 tests passed in the final full offline run (Python 3.13, 61.208 seconds).

## Store identity and outstanding work

Read-only inventory observed legacy `codeatlas_symbols`, UUID
`RAHAzCX7Tn2804qrSkpkCQ`, 4,340 documents, no inventory issues or deletion
candidates. Metadata inspection showed no pending sync/full/publication jobs.
These inspections are separate operations, not an atomic cross-store snapshot.
No live ingestion/reindex, publication, retention deletion, cache flush/rotation,
API restart, `.env` or application default change occurred.

Privileged packet interface inspection was retried noninteractively; sudo still
requires a password. No packets were captured and no kernel/forwarding settings
changed. Precise transport attribution remains blocked by tracing access.
[Remaining research](remaining-milestones.md) also retains cross-process sharing,
cross-store visibility and reader-safe rollback/retention as open work; passing
these checks does not implement those protocols.
