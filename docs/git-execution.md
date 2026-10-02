# Git subprocess execution

Repository ingestion and synchronization use a shared `git_output` runner for
clone, commit/branch inspection, fetch, reset and diff. Each command receives
`GIT_TIMEOUT_SECONDS` (default 120; positive integer). Set it before starting the
API/writer process when large or slow repositories need more time. The existing
environment and Git configuration are preserved except repository overrides
listed below and `GIT_TERMINAL_PROMPT=0`;
stdin is `/dev/null`. Arguments remain an argv list with no shell interpolation.

Timeouts raise `subprocess.TimeoutExpired`. Ingestion rolls back and removes its
reserved clone only before a commit attempt; the API reports the existing sanitized
Git failure 502. Ambiguous commit retention is unchanged. Sync propagates the
timeout through its rollback/lock-release handling and reports its stable 500;
metadata/checkpoints do not advance on failed Git commands. A reset may already
have changed the worktree; retry uses the existing checkpoint/diff workflow.
Existing pending publication jobs resume before fetching new work.

The timeout applies separately to each subprocess, not the complete API operation.
Python terminates the direct child on expiry. This is not process-tree supervision
or a hard wall-clock guarantee for descendant helpers holding pipes open. Credential
helpers/askpass programs, Git config, network redirects and remote execution policy
are not isolated by this runner. Terminal input is disabled; deployment-specific
credential mechanisms still require operator configuration. No application-level
automatic Git retries or host Git configuration changes were added.

168 offline tests pass. Tests verify command arguments, environment preservation,
terminal-input suppression, timeout propagation, ingestion reservation cleanup
and sync metadata/checkpoint preservation. Existing recovery/lock tests still pass.
No live Git network operation, clone/sync, corpus/index/cache/schema or process
changes were performed. `.env` stays unchanged; `.env.example` documents the new
optional setting. Retrieval quality/performance measurements remain inherited.

## Repository environment overrides

The child runner drops `GIT_DIR`, `GIT_COMMON_DIR`, `GIT_WORK_TREE`,
`GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
`GIT_NAMESPACE`, `GIT_PREFIX`, `GIT_GRAFT_FILE`, `GIT_SHALLOW_FILE` and
`GIT_REPLACE_REF_BASE`. These can select alternate repository state despite the
directory passed to Git; see Git's [environment documentation](https://git-scm.com/docs/git/2.43.0)
and [environment definitions](https://github.com/git/git/blob/master/environment.h).
Only child environments change; parent variables stay intact. Credential helpers,
user/system/local config and unrelated environment remain operator-controlled.
This is not a complete Git sandbox or configuration override isolation.

192 offline tests pass. Mocked checks cover all removed names and preservation
of parent/helper settings. A real local Git fixture initializes two temporary
repositories, sets conflicting parent git-dir/worktree/index overrides, and
verifies rev-parse selects the requested repository and status creates no external
index. Temporary fixtures are removed. No network/live clone/sync, corpus/cache,
.env or running processes changed; previous quality metrics remain inherited.
Repository discovery is constrained by the metadata policy below.

## Clone-owned Git metadata

New sync and clone commit/branch inspection require an actual unlinked `.git`
directory. Missing directories, symlinked metadata, `.git` redirect files and
`commondir` redirects (including dangling links) are rejected before Git runs.
Repository commands also pass explicit absolute `--git-dir` and `--work-tree`
paths, so they do not fall back to discovering a parent repository. This service
supports its ordinary shallow clones, not linked worktrees or shared metadata.
Unsafe metadata uses the existing sanitized 409 on sync; inspect it rather than
removing/reconstructing metadata automatically. Ingestion failures during initial
Git inspection retain pre-commit reserved-clone cleanup.

Already committed pending sync publication resumes before metadata checks,
because it publishes stored symbols without consulting Git. Full symbol indexing
still operates on its recorded file snapshot; it does not invoke Git discovery.
196 offline tests pass: real temporary Git fixtures verify chosen worktrees and
parent-repository exclusion, while sync tests verify no mutation/Git call on
missing metadata and pending replay without metadata. Read-only live audit found
all six registered clones compatible. No live Git/network/sync, rows, clones,
cache/index, settings or processes changed; retrieval metrics are inherited.

This validates metadata layout, not every metadata entry or repository integrity.
Symlinked refs/objects/config, malicious Git configuration and concurrent local
replacement remain outside this scoped check and require trusted clone ownership.

## Lossless incremental diff paths

Production sync requests `git diff --name-status -z` and requires NUL-delimited
records. Tabs/newlines, quotes, spaces and Unicode in decoded paths are preserved
instead of splitting line/tab output or indexing Git's quoted spelling. Rename
records retain both paths; copy records add the destination without removing
the source. A/M/D/T records retain their status. Unknown/truncated/empty path
records fail before worktree reset, metadata mutation or checkpoint advancement.
The standalone parser keeps its old line-format interface for existing callers;
production never falls back to it. Existing committed journals replay unchanged.

201 offline tests pass. A real temporary Git repository verifies tab/newline
name-status output; mocked sync verifies exact metadata/deletion-journal paths
and malformed records stopping before reset/checkpoint. Parser tests cover
renames/copies and preserved whitespace. CPU/one-thread live evaluation validates
25 cases and reproduces all baseline metrics, hybrid Recall@10 .880 / MRR .499.
No live sync, reindex, corpus/cache/settings/process changes or performance claim.
Git stdout remains text-decoded; filenames that cannot be decoded fail safely
rather than receiving a lossy replacement. Binary filename support is separate.
