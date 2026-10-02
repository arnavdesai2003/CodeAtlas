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

This is a scoped webhook fix. It adds no replay/delivery deduplication, durable
background queue, body-size bound or authorization for repository CRUD/search
routes. Keep local API servers on loopback; public deployment controls remain
separate work.
