# Repository mutation errors

Repository mutation routes return stable messages rather than exception text.
Git stderr, local clone paths, backend URLs and internal parsing/commit details
are excluded from failure responses. Successful response schemas are unchanged,
including the existing creation `local_path` field; this milestone addresses
error disclosure, not a public deployment authorization boundary.

| Operation and condition | HTTP status | Meaning |
|---|---|---|
| Create: invalid GitHub URL | 400 | Correct the URL before retrying. |
| Create: already registered or clone directory occupied | 409 | Inspect registration and clone state; do not delete a possibly committed clone. |
| Create: Git subprocess failed | 502 | Clone/initial Git inspection failed. |
| Create: other failure | 500 | Inspect metadata and clone state before retrying; commit outcome can be ambiguous. |
| Sync: repository absent | 404 | No registered repository with this ID. |
| Sync: expected repository clone absent | 409 | Inspect local clone state. |
| Sync: writer contention or pending publication/full indexing | 409 | Finish/retry the owning work before synchronization. |
| Sync: other failure | 500 | Retry synchronization to resume any pending target; investigate repeated failures. |

Previously every creation `ValueError` returned 409, Git stderr returned 400,
and every sync `ValueError` returned 404. Typed exceptions now distinguish
expected conditions from unrelated parser, model, filesystem or transaction
failures. Invalid/malformed URL parsing is rejected before clone/database work.
Direct Python callers retain useful exception causes/details; known errors
subclass the previous built-in exception families for compatibility.

Transaction boundaries, rollback, advisory locks, directory reservation, pending
journals and ambiguous-commit clone preservation are unchanged. Pending full-index
work now uses the existing coordination exception family and returns 409 instead
of 500. No automatic destructive cleanup, HTTP retries or job deletion was added.
See [sync recovery](sync-recovery.md) and
[full-index recovery](full-index-recovery.md) before reconciling failed work.

158 offline tests pass. New API checks cover typed statuses, sanitized messages,
internal failures retaining 500, and actual malformed-URL rejection. Existing
transaction/failure tests continue verifying clone preservation, reservation races,
pending-job recovery and locks. No live ingestion/sync was invoked, and no data,
schema, corpus/index/cache, settings or running API processes changed. Retrieval
code and performance are unchanged; quality measurements remain inherited.

## Accepted repository URL profile

Inputs must be HTTP/HTTPS URLs with the exact authority `github.com` or
`www.github.com` (case-insensitive), and a path `/owner/repository` with an
optional single trailing slash. Owners use ASCII letters, digits, underscores
or hyphens; repository components also permit periods. A terminal `.git` is
removed for local directory naming. Dot/dot-dot repository components after
that removal are rejected. This is CodeAtlas's input profile, not a complete
implementation of GitHub's naming rules.

Credentials, explicit ports (including 443), query strings/fragments, percent
escapes, backslashes, repeated separators, whitespace/control characters and
non-ASCII components are rejected with 400 on creation. Controls are checked
before URL parsing can silently remove them. Accepted text is preserved for
Git and database storage; URLs are not automatically canonicalized or merged.
Equivalent accepted spellings can still conflict on their shared clone directory.

Offline tests verify rejection before any database lookup, mkdir or Git call.
A read-only live check validated all six registered repository URLs; no rows
or clones were modified. Older stored URLs outside this profile need operator
inspection before a new sync can parse them; existing pending sync publication
still resumes before URL parsing. Do not rewrite stored URLs or remove clones
automatically. This validation does not constrain Git redirects, credential
helpers or local Git configuration. Source-file symlinks are now excluded by
the [source selection policy](source-paths.md), with its documented limits.
