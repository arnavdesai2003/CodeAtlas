# Offline hardening regression validation

Verified on 2026-10-04 with the repository's Python 3.13 virtual environment:

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Before new coverage, all 275 existing tests passed. Added 29 regression tests;
the final suite passed all **304 tests**. Subtests exercise additional malformed
values without being counted as separate tests. Only `tests/` was collected;
manual `scripts/test_*` integrations were not executed.

| Boundary | Verified behavior |
| --- | --- |
| Elasticsearch wrappers | Actual installed `ObjectApiResponse` objects work through alias resolution, full publication and incremental deletion. Malformed bodies are rejected. |
| Sync recovery | Invalid deletion completion preserves the Git checkpoint, pending job and committed symbol IDs. A valid retry resumes without parsing again. |
| Full publication | Malformed copies stop before target writes/alias switches. Ambiguous alias acknowledgements retain ready work; retry finalizes the active stage without rebuilding. Missing generations cannot be provisioned by incremental writes. |
| Retention | Malformed tasks, statistics, aliases and policies stop cleanup. Ambiguous deletion leaves audit state intact; retry reconciles absence. Wrapped metadata/deletions support idempotent retry. |
| Cache | Duplicate JSON becomes a generation-bound miss and refills normally. Invalid Redis replies/tokens disable fills/coalescing. Cleanup escapes glob characters in old tokens. Existing stale-fill interleaving tests also pass. |
| HTTP | Invalid routing/completion metadata produces sanitized 503 responses without fills or retained flights. Redis health requires positive acknowledgement. Signed duplicate or excessively nested webhook JSON rejects before lookup/scheduling; signature verification remains first. |
| Plan loading | Duplicate fields reject at any object level; CLI rejection precedes apply and does not echo private field names. |

Tests use mocks, temporary SQLite transactions, local Git fixtures and in-process
HTTP clients. No model downloads, live corpus changes, generation rotations,
publication, cleanup or API restarts were performed. No production code changed
in this validation milestone.

An initial new fixture failed because a client captured in a function default
escaped the mock and attempted a sandbox-blocked local connection. It was fixed
to inject the mocked client explicitly. A first nested-JSON fixture was below
the installed decoder's recursion capacity; the final test uses a real signed
10,000-level payload within its test body limit and verifies the specific JSON
error response. These preliminary failures are excluded from the final pass.

These are offline behavioral checks, not live Elasticsearch/Redis protocol
verification, PostgreSQL concurrency validation, retrieval evaluation or
performance measurements. Glob coverage checks the exact escaped Redis scan
pattern; it does not execute a Redis server's matcher. Prior quality/performance
measurements were not rerun and are not new results of this milestone.

Subsequent [isolated Redis verification](cache-consistency.md#isolated-redis-protocol-verification-2026-10-04)
exercised the real Lua protocol and glob matcher, including a post-rotation refill
and four metacharacter/decoy cases. It adds five offline command failure/cleanup
tests, bringing the suite to 309 passing tests. This follow-up does not add live
Elasticsearch/PostgreSQL or retrieval/performance validation.

The next [scratch publication protocol verification](atomic-publication.md#isolated-publication-protocol-verification-2026-10-04)
passed against real Elasticsearch/Redis with temporary SQLite metadata and
deterministic vectors. It adds nine offline safety/failure tests (318 total).
PostgreSQL concurrency and model quality/performance remain outside this check.

## Shared process-recovery fixtures (2026-10-04)

The later process-exit and ingestion-race coverage brings the offline suite to
367 tests. `tests/recovery_support.py` owns file-backed SQLite sessions with
foreign keys enabled, bounded spawn/exit assertions and termination/kill/join
cleanup for owned children. `tests/publication_process_fixture.py` contains the
disk-backed simulated publication side effects. Recovery modules no longer
import fixtures from other test modules, including the retention module's
environment-mutating setup. The existing 367 tests pass after consolidation;
no scenarios or assertions were removed. README now summarizes current coverage
and links the detailed protocol/recovery documents.

This milestone changes test organization only. No application behavior, live
services/data/settings, retrieval evaluation or performance claims changed.

## Verification entrypoint argument safety

The Redis cache, scratch publication, PostgreSQL writer-lock and cross-process
writer-lock commands now parse arguments before invoking their probes. `--help`
exits 0 without scratch creation, lock acquisition or owner-process spawning;
unsupported flags/positionals exit 2. Their no-argument verification and cleanup
behavior is unchanged. Previously arguments were ignored and could enter a probe
even when help was requested.

Two offline command regressions cover all four entrypoints. Existing verifier
failure/cleanup checks still pass with explicit empty argument lists; all 404
offline tests pass. Four actual module `--help` calls exited safely. No protocol
probe rerun, scratch mutation, live locks/data/settings/API changes, retrieval
evaluation or performance measurement occurred. Earlier live protocol results
remain prior measurements, not newly verified here.
