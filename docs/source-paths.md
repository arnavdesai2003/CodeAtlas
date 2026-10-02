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

162 offline tests pass. Fixtures cover internal/external/dangling file links,
linked directories, absolute/traversal paths, discovery, full symbol skipping,
regular-file-to-symlink sync and deletion-failure replay. CPU/one-thread live
evaluation had 25 valid cases and reproduced all baseline metrics, hybrid
Recall@10 .880 / MRR .499. Live corpus was not reindexed; data, schema, caches,
settings and API processes were unchanged. No latency or throughput claim.

These checks assume stationary generated clones under trusted directory roots.
They do not protect against concurrent malicious filesystem replacement between
selection and open, hard links, symlinked ancestors above the clone, or Git
redirect/configuration behavior. Do not edit generated clones during indexing.
This is not a sandbox for hostile local writers or public API deployment.
