# Search failure behavior

Both lexical and semantic requests set `allow_partial_search_results=false`.
CodeAtlas also checks returned `timed_out` and `_shards.failed` fields before
formatting hits. Either signals an incomplete result and raises
`IncompleteSearchError`; hybrid fusion does not substitute the surviving branch.
Elasticsearch documents these partial-response conditions in its
[search API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-search).

`POST /search` returns HTTP 503 with
`{"detail":"Search backend unavailable."}` for Elasticsearch API/transport
errors and incomplete search responses. Backend response bodies, URLs and index
names are excluded. This covers alias resolution as well as retrieval. Unexpected
application/model errors retain the ordinary HTTP 500 behavior; they are not
classified as transient Elasticsearch outages. No automatic API retries or
`Retry-After` promise were added. The existing Elasticsearch client retry/timeout
policy is unchanged; 503 does not imply a bounded response deadline.

Failed retrieval never writes a cache entry or publishes successful shared work.
Existing coalescing propagates the exception to joined waiters and releases its
slot; subsequent requests can retry. Redis failures still fall back to retrieval.
A valid existing Redis hit can succeed while Elasticsearch is unavailable;
`/health` continues reporting dependency health independently. Genuine complete
empty searches still succeed and may be cached. Benchmark bypass failures remain
failures, rather than empty successes.

Offline tests inject timeouts/failed shards into both branches, verify no cache
fill, released flight and successful retry, and validate sanitized API errors.
Existing deterministic coalescing tests cover leader-error propagation to waiters.
145 offline tests pass. CPU/one-thread live multi-repository evaluation validated
all 25 cases and reproduced every retrieval metric, hybrid Recall@10 .880 and
MRR .499. No corpus, mappings, model, candidates or ranking changes were made.

This milestone hardens search failures only. Repository mutation routes and
public deployment authentication/authorization remain separate work. Use loopback
for local development; this change does not make the API ready for public exposure.
