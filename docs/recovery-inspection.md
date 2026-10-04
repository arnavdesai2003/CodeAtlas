# Metadata-only recovery inspection

```sh
.venv/bin/python -B -m scripts.inspect_recovery_jobs
```

The command reads sync, full-index and publication journals without contacting
Elasticsearch or Redis, taking advisory locks, initializing schema or changing
rows. PostgreSQL inspection runs in a repeatable-read, read-only transaction;
session closure rolls it back. SQLite is supported only for offline fixtures.
Missing tables, database outages or inspection errors produce sanitized failure
output and nonzero exit rather than an empty success report.

Output includes repository IDs, recorded commit targets, path/file counts and
publication phases/index names. Clone URLs, source paths, code and raw statistics
are omitted. A pending publication blocks other cooperating writers. The report
suggests owner publication retry first and withholds other work while that journal
exists. Conflicting sync/full jobs or unknown publication phases have no resume
hint; inspect and reconcile their state without deleting journals manually.

Hints are informational, never executed. An observed journal is not proof that its
dependencies are ready or its indices intact. Metadata can change after the
snapshot; actual retry commands recheck state and acquire production writer locks.
Use [sync recovery](sync-recovery.md) and [atomic publication](atomic-publication.md)
for the relevant workflows. Generation inspection remains necessary for index
identity/lifecycle checks and retention; this command does not replace it.

On 2026-10-04 a real local PostgreSQL run found no pending jobs. Seven offline
tests cover job counts, resume ordering/conflicts, unknown phases, read-only
transaction setup, no commits and sanitized failure/session closure. The full
offline suite passes 388 tests. No schema/data/index/cache/settings/API changes,
retrieval evaluation or performance measurement occurred.

## Malformed metadata and CLI arguments

Inspection requires sync paths to be a list of nonempty strings and file IDs to
be a list of positive int32 integers. Strings, mappings, JSON null, malformed
elements or invalid ID values fail inspection before counts or resume hints are
reported. Empty lists remain valid for deletion-only or empty changes. Failure
output omits raw metadata; it never repairs or deletes malformed journal rows.

`--help` exits before session creation; unsupported arguments return argument
error/exit 2 before database access. Four new offline regressions bring the suite
to 392 passing tests, including malformed persisted JSON containers/elements and
CLI access ordering. No live database inspection or mutation was needed for this
follow-up; the earlier PostgreSQL observation remains a prior result.

## Explicit blocked reasons

Every job now has `blocked_reason` alongside `resume`. An unblocked metadata
state uses null; a blocked state withholds the command and explains why:

| Reason | Meaning |
| --- | --- |
| `pending_publication_blocks_sync` | Finish the owning publication before sync. |
| `another_repository_owns_publication` | Full work must wait for the publication owner. |
| `conflicting_sync_and_full_jobs` | Both journal kinds exist for this repository; inspect their history. |
| `unknown_publication_phase` | Publication phase is unsupported; do not guess a retry. |
| `publication_owner_has_pending_sync` | Publication and sync journals conflict on the owner. |

The root report separately lists publication/sync owner conflicts, including
cases without a full-index job. Reasons describe the metadata snapshot and do
not establish dependency readiness. No automated reconciliation was added.
An owner-conflict regression and strengthened existing checks bring the suite
to 396 passing offline tests. This follow-up did not access or change live data.
