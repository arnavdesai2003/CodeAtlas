# API key access

Set a private `API_KEY` before starting API processes. Send it as
`X-CodeAtlas-API-Key` for search, repository listing, creation and synchronization.
One key grants all of those operations; this is not per-user authorization.
Configured keys are required in every environment, including development/test.
Missing/wrong keys return 401 before route/service/database work. Comparison uses
constant-time HMAC comparison; the settings field redacts its value in repr.
Do not log, publish, commit or put the key in query strings.

With no key, only `APP_ENV=development` or `test` retain local unauthenticated
behavior. All other environment values fail closed with 503
`API authentication is not configured.` Restart processes to load changes.
Neither `.env` nor existing processes were changed by this milestone. Local
development remains loopback-only. Use encrypted transport and controlled secret
distribution before exposing a keyed service; the application does not add TLS.

`/health`, `/`, API docs/OpenAPI remain public. Health reports sanitized dependency
state. Webhooks retain independent HMAC authentication and are not gated by the
API key. Body-size/receipt checks run before authentication; malformed/oversized
bodies can return their existing errors first. A valid API key does not enable
benchmark bypass: environment, opt-in and loopback restrictions still apply.
Custom clients must supply the header when enabling a local key.

187 offline tests pass. Checks cover every search/repository operation failing
closed outside local modes, wrong/missing keys, accepted exact-text searches,
public health, secret redaction and unchanged benchmark bypass restrictions
after authentication. No live data/cache/settings/process changes, and no new
retrieval/performance measurements. Existing metrics are inherited.

No multi-key rotation window, scopes, rate limits, aggregate concurrency controls,
access audit log or identity provider was added. Restart every process together
for rotation; old processes retain old settings. This is a limited application
access boundary, not a complete public deployment configuration.

## Authenticated local benchmarks

`scripts.benchmark_api` and `scripts.benchmark_http_miss_burst` now read the
explicit client variable `CODEATLAS_BENCHMARK_API_KEY`. Set it to match the
server's `API_KEY`; do not put a key in command-line arguments. The clients
never automatically use server `API_KEY` or read server secrets from .env for
authentication. With the client variable unset/empty, behavior stays keyless.

Keyed targets must use HTTP/HTTPS loopback IPs or localhost, with no URL user
credentials. Nonloopback destinations are rejected before HTTP requests.
Both clients disable redirects and proxy environment handling. Client keys must
be printable ASCII without whitespace/control characters; validation errors do
not echo the supplied value. Cached and guarded uncached requests retain the
authentication header, including when adding the bypass header. All server-side
benchmark bypass restrictions still apply; a key does not relax them.

190 offline tests pass. New checks cover server-key independence, destination/
header rejection and HTTPX default-header merging. An isolated one-worker
CPU/one-thread HTTP smoke generated a matching key only in parent/child process
environments: all 80 small-body searches passed attribution/equality checks.
Temporary server stopped, private Redis namespace removed, legacy routing/index
UUID/4,340 docs unchanged. No .env/live generation/port 8000 changes; no new
retrieval evaluation or benchmark capacity claim. Authentication-enabled archived
controls require source versions that support the same server key policy.
