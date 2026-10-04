# GitHub webhook handling

Configure a nonempty `GITHUB_WEBHOOK_SECRET` matching GitHub's webhook secret
and restart the API to load it. An unset or empty value disables
`POST /webhooks/github`: HTTP 503, `GitHub webhook is not configured.` This
includes requests signed with an empty key, which previously passed verification.
The example environment value is a local setup example; choose your own secret
for a deployed installation.

Authentication checks the HMAC-SHA256 signature against the exact request body
before JSON parsing or database access. Missing, invalid or non-ASCII signatures
return HTTP 401. Signed invalid JSON, a non-object root, or a push payload with
a non-object repository/missing/non-string/blank clone URL return HTTP 400.
Rejected requests do not look up repositories or schedule background sync.

Signed JSON that exceeds the decoder's nesting capacity also returns the same
HTTP 400 `Invalid webhook JSON.` response. Decoder `RecursionError` is handled
before database access or background scheduling; signature verification still
comes first. This adds no new nesting limit and leaves valid event behavior
unchanged. No tests were added or run for this follow-up.

Valid `ping` and unsupported events retain HTTP 202 with `ok`/`ignored` bodies
and schedule no work. A valid push must match a registered repository's exact
clone URL; otherwise it returns 404. Accepted pushes return 202 and schedule
that repository's existing background synchronization. Acceptance does not
mean synchronization completed. Existing journal recovery and locking still
govern the synchronization task; retry/recovery behavior is unchanged.

153 offline tests pass. Eight webhook tests verify empty-secret rejection,
signature failures before parsing, malformed authenticated payloads, ping/ignored
events, accepted registered pushes and unknown-repository rejection with session
closure. Services and background synchronization are mocked: no external delivery,
clone, live database write, corpus change or cache invalidation was performed.
Retrieval code is unchanged, so the preceding quality evaluation is inherited.

The assembled API now enforces a [request body limit](request-limits.md) before
webhook verification. The default 1 MiB limit returns 413 for oversized payloads,
including unauthenticated/unconfigured requests; exact accepted bytes retain
their signature. Configure a larger limit explicitly for larger valid events.

This is a scoped webhook fix. It adds no replay/delivery deduplication, durable
background queue or authorization for repository CRUD/search
routes. Keep local API servers on loopback; public deployment controls remain
separate work.

## Background session lifecycle (2026-10-02)

Session creation now occurs inside the background task's exception handler.
Sync failures and session creation/close failures are recorded through the
`app.api.webhooks` logger with repository ID and exception type, without raw
exception text, traceback or result payload. Completion is an INFO record;
failures are ERROR records. Configure logging levels/collection to retain the
records needed operationally. A completion followed by a close-failure record
means sync returned successfully but session cleanup failed.

If a session was created, close is attempted in finally, including after sync
failure. Ordinary close failures are logged without escaping the accepted task;
this does not guarantee a failed close released every resource. Process/task
termination remains outside ordinary Exception handling. Sync's existing rollback,
locks and pending-job recovery are unchanged.

216 offline tests pass, including session creation, sync and close failures with
sanitized captured logs. No live deliveries/sync/DB/cache/process changes;
retrieval/performance checks inherited. Acceptance still means scheduled, not
completed. In-process tasks can be lost on process exit, and failures before a
sync journal exists require manual retry; there is no durable delivery queue or
automatic retry. Use the normal authenticated sync route to resume pending work.
