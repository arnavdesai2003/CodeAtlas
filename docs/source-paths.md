# Source-file selection

Discovery, full symbol parsing and incremental synchronization select regular
files through `regular_source_path`. Absolute/parent-traversal paths, `.git`
components, symlinked files and symlinked parent directories within the clone
are excluded. Internal links are excluded too; indexing does not read their
targets even when those targets remain inside the clone. Dangling links,
directories and missing files are also excluded.

Previously discovery's `is_file()` and indexing's `read_text()` followed source
symlinks, allowing external content to be hashed or parsed. File-selection checks
now precede those reads. Supported source extensions and Python-only symbol
parsing remain unchanged.

Full symbol replacement skips excluded files and removes their old symbols from
the committed snapshot; existing file metadata remains until discovery/sync
reconciles it. For affected supported paths during incremental sync, an excluded
file removes existing metadata and symbols. The affected path remains in the
durable sync job so Elasticsearch removes stale documents before cache rotation
and checkpoint advancement. Failed deletion resumes through the same journal.
This also reconciles affected missing/non-regular files rather than leaving
old metadata behind. Previously committed pending jobs are replayed unchanged;
this does not scrub content already indexed or cached. Rebuild/sync through the
normal recovery workflows when reconciliation is needed.

165 offline tests pass. Fixtures cover internal/external/dangling file links,
linked directories, absolute/traversal paths, discovery, full symbol skipping,
regular-file-to-symlink sync and deletion-failure replay. CPU/one-thread live
evaluation had 25 valid cases and reproduced all baseline metrics, hybrid
Recall@10 .880 / MRR .499. Live corpus was not reindexed; data, schema, caches,
settings and API processes were unchanged. No latency or throughput claim.

These checks assume stationary generated clones under trusted directory roots.
They do not protect against concurrent malicious filesystem replacement between
selection and open, hard links, symlinked ancestors above the configured root, or Git
redirect/configuration behavior. Do not edit generated clones during indexing.
This is not a sandbox for hostile local writers or public API deployment.

## Clone-directory checks

Ingestion, new synchronization and full symbol replacement use `clone_directory`
before clone/Git/file reads. The configured repository root, owner directory and
clone itself must not be symlinks (including dangling links) or non-directory
objects. Missing components remain allowed for ingestion's atomic reservation;
missing clones retain the existing failure behavior in indexing/sync. Rejected
directories are never automatically removed or repaired. Creation/sync return
a sanitized 409: `Repository directory is unsafe; inspect local state.`

Pending sync/full work still resumes before clone checks because it publishes
already committed metadata without rereading Git/source. Lock/rollback and
ambiguous-commit retention rules are unchanged. Offline fixtures verify redirected
owners/clones cannot invoke Git or parse source, external files remain intact,
all three levels reject links/files, and API responses exclude private paths.
Ancestors above the configured root are trusted; operators must inspect them.
The check does not provide race-free directory handles or protection against a
hostile local writer replacing components after validation.

## Real Git type-change recovery

203 offline tests pass. A new real local Git fixture commits regular-file →
symlink → regular-file transitions. Git refs/diff/reset execute normally; network
fetch and Elasticsearch/Redis are mocked, with temporary SQLite metadata. The
first sync resets to a link, removes old metadata/symbols and records the exact
path. Injected deletion failure leaves the old checkpoint and pending job; retry
resumes publication. A subsequent regular-file transition is indexed again, and
the external symlink target remains unchanged. Temporary repos/data are removed.

Existing metadata refreshed by Git's `T` status now contributes to files_modified,
as `M` already did. A previously excluded link becoming an indexed regular file
still counts as files_added; a regular file becoming excluded counts as deleted.
This fixes reporting only. Selection, journal/checkpoint rules, ranking and live
data remain unchanged. No new live quality or performance measurement was taken;
the preceding evaluation is inherited.

## Rename and copy replay coverage (2026-10-02)

205 offline tests pass. A real temporary Git fixture renames a regular source
to a filename containing a tab/newline, then executes actual refs/diff/reset
with rename detection explicitly enabled in the test. Mocked publication failure
leaves both exact paths in the journal, old metadata removed, new symbols stored
and the Git checkpoint unchanged. Retry resumes publication without Git calls
and advances to the recorded target. Network fetch and external stores remain
mocked; no live repositories are changed.

A separate deterministic C100 fixture verifies copy handling: source file and
symbol IDs remain intact; only the destination is affected/indexed/journaled,
files_added increments and files_renamed stays zero. Retry after indexing failure
does not reparse or delete source metadata. This tests consuming a copy record,
not automatic Git copy detection. Production detection/configuration remains
unchanged. No production code fix was needed; ranking, quality evaluation and
performance measurements remain inherited from earlier milestones.
