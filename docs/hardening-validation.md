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
