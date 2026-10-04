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

## Finite retrieval scores (2026-10-02)

Elasticsearch hit formatting now rejects non-numeric/bool scores, NaN/infinity
and numeric conversion overflow. Missing/null scores retain their prior zero
value. Finite negative/zero/positive numeric scores retain their existing values.
Normalization rejects non-finite inputs and an overflowing finite score range
rather than silently emitting invalid or collapsed fusion scores. Ordinary
min-max fusion, equal-score handling, weights and candidate sizes are unchanged.

These failures raise InvalidSearchResponseError through the existing sanitized
503 path. They never reach successful cache fills or shared results; flight
cleanup/retry behavior remains unchanged. This validates retrieval scores, not
every backend field or optional reranker model output. No live invalid-score
incident was observed; the guards defend the response boundary without masking
bad scores as zero or falling back to one retrieval branch.

222 offline tests pass, covering score types/non-finite/overflow, finite/null
compatibility, normalization, no cache writes/retained flights and sanitized API
errors. Live CPU/one-thread evaluation: 25 valid cases, every baseline reproduced,
hybrid Recall@10 .880 / MRR .499. Isolated one-worker HTTP smoke: 80 successful
queries with payload/attribution checks; temporary server/private keys cleaned,
legacy routing/index UUID/4,340 documents unchanged. No live corpus/settings/
process changes or performance improvement claim.

## Invalid query embedding output

Semantic retrieval validates model output before submitting its Elasticsearch
vector search. It shares indexing's vector checks: a 384-dimensional list of
finite numeric values, excluding booleans and strings. Invalid shape, nonfinite
values or numeric overflow raise `InvalidQueryEmbeddingError`, handled by the
existing sanitized HTTP 503 response. This is a backend output failure, not
invalid user query text. Valid vectors pass through unchanged.

Failures do not fill Redis, and process-local miss-coalescing state is released
so a later request can retry. Parallel lexical work may already be running;
hybrid retrieval still fails rather than returning only that branch. Existing
valid cache hits need no new embedding. The check does not validate vector
normalization or all Elasticsearch similarity constraints. Offline tests cover
malformed output, no vector-search submission/cache fill, clean flight state,
healthy retry and sanitized API errors. Real-query evaluation retained every
baseline metric. No invalid live model output was observed.

## Invalid optional reranker output

The optional cross-encoder now requires exactly one finite numeric scalar per
candidate before constructing or sorting results. Short/extra batches, non-scalar
output, booleans, strings, nonfinite scores and numeric overflow raise
`InvalidRerankerOutputError` instead of silently truncating candidates or sorting
invalid values. NumPy numeric scalars remain supported; valid score conversion,
stable tie order and result limits remain unchanged. Candidate dictionaries are
not modified. Empty candidate sets still skip model loading.

The typed error participates in existing sanitized API backend-error handling;
the default `/search` path still does not invoke reranking. Offline tests cover
bad batches/scores, healthy retry, stable ordering and sanitized responses. Live
25-case evaluation reproduced all metrics, including reranked Recall@10 .800 /
MRR .493. No malformed live cross-encoder output was observed.

## Optional reranker cold initialization

A process-local lock now serializes cached cross-encoder initialization, matching
the embedding loader's approach. Concurrent cold requests share one constructed
model; an initialization exception releases the lock and is not cached, allowing
later calls to retry. The lock ends before `predict`, so inference remains
concurrent. Each worker process still loads its own model; this is not a global
model pool or inference thread-safety guarantee.

Offline event/barrier tests cover six cold callers, failed initialization retry
and overlapping predictions without loading real models. Real 25-case evaluation
reproduced reranked metrics. No concurrent production cold-start incident or
throughput improvement was measured. Default API reranking stays disabled, and
model/device/thread settings are unchanged.

## Malformed Elasticsearch hits

Retrieval now checks the hit collection and each source document before constructing
results. Required textual fields must be strings; line numbers must be integer
values with a positive start and end at least start. Optional language remains
nullable or textual, and the test flag must be boolean when present. Missing
language/test flag retain their existing defaults. Invalid collections, missing
sources or wrong field types raise `InvalidSearchResponseError` through the
existing sanitized HTTP 503 handler instead of raw lookup failures or malformed
successful results.

Failure in any hit rejects that retrieval result set; no partial cache fill or
surviving hybrid branch is returned. Miss-flight state is released for retry.
Valid hit formatting, scores, ranking and source text remain unchanged. This
validates newly retrieved hits, not the full schema of existing Redis entries,
and does not certify source-content correctness or repository permissions.
Offline tests cover collections, required/optional fields, line ranges and
cache/retry behavior. Live evaluation reproduced all baseline metrics with 25
valid cases; no malformed live documents were observed or repaired.

Retrieval unwraps Elasticsearch response bodies before validating their shape.
When reported, `timed_out` must be boolean and `_shards` must be a mapping with
a nonnegative integer `failed` count. Malformed falsey values raise
`InvalidSearchResponseError`; true timeouts or positive shard failures retain
`IncompleteSearchError`. Both follow the existing sanitized 503/cache-fill
failure path. Omitted completion fields retain prior compatibility; this is
validation of reported metadata, not a new completeness guarantee for omitted
fields. Valid ranking and result formatting are unchanged. No tests were added
or run and no live evaluation was performed for this follow-up.
