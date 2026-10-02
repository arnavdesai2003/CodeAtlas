# Git subprocess execution

Repository ingestion and synchronization use a shared `git_output` runner for
clone, commit/branch inspection, fetch, reset and diff. Each command receives
`GIT_TIMEOUT_SECONDS` (default 120; positive integer). Set it before starting the
API/writer process when large or slow repositories need more time. The existing
environment and Git configuration are preserved, except `GIT_TERMINAL_PROMPT=0`;
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
