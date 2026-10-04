# PostgreSQL writer coordination verification

Run against the configured development PostgreSQL database:

```sh
.venv/bin/python -B -m scripts.verify_writer_locks
```

The command uses production repository/publication/maintenance lock contexts
and three independent metadata sessions. Random positive int32 repository IDs
are checked against registered repositories. Pending sync, full-index or
publication jobs block the probe; an occupied corpus lock fails immediately.
No schema initialization or metadata writes occur. Metadata commits contain
only reads; they test that the dedicated advisory connection retains its locks.

The probe briefly acquires the actual corpus coordination lock. Run while
writers are idle: cooperating writers may receive their normal conflict response
during verification. Readers do not participate in these locks. Preflight is
not a reservation of idle state; concurrent work can make the probe fail.
Do not run this diagnostic against an unsupported database: SQLite's no-op
lock implementation cannot establish PostgreSQL coordination.

Four groups verify shared writers on different repositories, same-repository
exclusion, publisher and maintenance exclusion across metadata commits, partial
acquisition cleanup, and release after controlled exceptions. Unexpected backend
errors fail verification rather than being counted as contention. Sessions close
on ordinary success/failure. Abrupt process termination releases session locks
when PostgreSQL detects the disconnected clients.

On 2026-10-04 all four groups passed against local PostgreSQL. Seven offline
guard/failure tests bring the full offline suite to 325 passing tests. No metadata
rows, schema, corpus/index/cache, settings or API processes changed. This verifies
cooperating process lock behavior, not crash recovery, cross-store atomicity,
legacy/external writers, sustained concurrency or retrieval quality. No performance
claim follows from the result.
