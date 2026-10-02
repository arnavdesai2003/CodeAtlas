# HTTP request body limit

The assembled API applies `RequestBodyLimit` to all HTTP requests before route
dispatch. `REQUEST_BODY_MAX_BYTES` defaults to 1,048,576 bytes (1 MiB) and must
be a positive integer. Set it before starting the API and restart to change it.
Large legitimate webhook payloads require an explicitly larger limit.

The middleware counts actual ASGI body bytes across chunks, regardless of a
missing or misleading `Content-Length`. It stops receiving at the first chunk
that crosses the limit and returns HTTP 413 with
`{"detail":"Request body too large."}`. No route JSON parsing, signature check,
database access, background scheduling or retrieval occurs for that request.
The limit takes precedence over route authentication/validation, including an
unconfigured webhook. Client disconnection during receipt does not dispatch work.

Accepted bodies are replayed byte-for-byte as one ASGI body message. Webhook
HMAC verification therefore uses the same original bytes, including whitespace
and non-ASCII bytes. An exact-limit body is accepted. Non-HTTP scopes pass through;
startup/lifespan behavior is unchanged. Existing query validation, result limits,
cache generation/coalescing and benchmark bypass remain unchanged.

175 offline tests pass. ASGI tests cover chunk boundaries, exact-limit byte
preservation, misleading headers, disconnect and lifespan passthrough. Webhook
tests confirm oversized requests perform no authentication/database work and
exact-limit signed whitespace retains its valid signature. An isolated loopback
HTTP smoke test used 80 successful small-body searches across four query mixes,
CPU/one-thread, one API worker; result/engine attribution checks passed. The
temporary server stopped, private Redis namespace was removed, and legacy index
routing/UUID/4,340 documents stayed unchanged. This was a functional smoke test,
not a throughput comparison. No live cache generation, corpus/schema/settings or
port 8000 process changes; prior retrieval evaluation remains inherited.

This bounds middleware accumulation, not transport/server chunk buffers,
aggregate memory across callers, response sizes, decompression, or CPU spent on
accepted JSON. There is no concurrency cap, rate limit or public
API authorization in this change. Hostile local/process behavior
need separate deployment controls.

## Search query profile

`POST /search` requires 1–4,096 Unicode characters and at least one character
that is not whitespace according to Python's string whitespace check. Overlong
and whitespace-only queries return 422 before cache/coalescing/retrieval work.
The maximum is published in OpenAPI. This is an API input bound, not a tokenizer
limit or a guarantee that a model uses every accepted character.

Valid input is preserved exactly: leading/trailing whitespace, case and Unicode
are not trimmed or normalized by validation. Existing cache-key normalization
and exact-text coalescing remain unchanged. The HTTP body-byte bound still applies
independently; 413 takes precedence when a body exceeds it. Direct Python engine
and service callers retain their existing interfaces and are responsible for
their own input policy. Search result limit stays 1–100, default 10.

178 offline tests pass. New tests cover the exact character boundary with Unicode
and surrounding whitespace, overlong ASCII/Unicode, whitespace-only text, no
service execution on rejection and the OpenAPI maximum. Retrieval/ranking code,
model/corpus/cache generation and running processes are unchanged. The prior
live quality/HTTP checks remain inherited; no new performance claim is made.

## Body receipt deadline

`REQUEST_BODY_TIMEOUT_SECONDS` defaults to 30 seconds and must be finite and
positive. Restart the API to change it. One deadline covers total body receipt,
starting when middleware begins receiving; chunks do not restart the clock.
Partial/stalled uploads return 408 with
`{"detail":"Request body receive timeout."}` without dispatching route work.
Oversized bodies still return 413 when observed before expiry. Legitimate slow
uploads may require an explicitly longer receipt deadline.

The timeout ends before route dispatch. It is not a search, sync, whole-request,
connection-idle or response-send deadline; Git/Elasticsearch policies remain.
External task cancellation propagates instead of becoming 408. Receipt assumes
cooperative ASGI behavior; blocked event loops/proxy buffers remain separate.
181 offline tests pass, including partial-body cancellation/no dispatch, route
execution outside the timer and external cancellation. No live data/settings/
process changes or new performance measurements; prior live checks are inherited.

## Real-socket receipt verification

Run `.venv/bin/python -B -m scripts.verify_http_receipt`. This starts a minimal
diagnostic FastAPI fixture with the production middleware on an ephemeral
loopback port, 16-byte size limit and 0.2-second receipt deadline. It uses no
database, Redis, Elasticsearch, models or production routes. Never deploy the
probe factory. Its child server stops on success/failure; nonzero exit invalidates
verification and the final completed record confirms shutdown.

Six real Uvicorn/socket cases passed: partial Content-Length and unterminated
chunked bodies returned 408; oversized fixed/chunked bodies returned 413; exact
16-byte fixed/chunked bodies returned 200 with unchanged byte counts. A handler
counter stayed zero after all rejections and advanced only for the two accepted
requests. A fresh stats request succeeded after each case. The two stalled cases
took 202.680/202.207 ms including a subsequent stats request; these are diagnostic
observations, not hard scheduling guarantees or production latency measurements.
The checks do not establish same-connection reuse or proxy behavior. 181 offline
tests still pass. No live stores, settings or API processes were touched.
