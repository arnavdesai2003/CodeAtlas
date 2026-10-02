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
accepted JSON. There is no receive deadline, concurrency cap, rate limit or public
API authorization in this change. Slow clients and hostile local/process behavior
need separate deployment controls.
