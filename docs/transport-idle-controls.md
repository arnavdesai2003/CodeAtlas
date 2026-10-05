# Inter-request idle controls — 2026-10-05

`profile_search_transport` now optionally waits between reading a complete BM25
response and resolving the alias on its pooled Elasticsearch client. This tests
whether the observed forwarding delay depends on residual connection state that
changes during idle time. It is diagnostic code only: no production sleep,
transport default, query, ranking, source-field or candidate change.

```sh
.venv/bin/python -B -m scripts.profile_search_transport \
  --samples 20 --warmups 5 --idle-delays-ms 0 1 5 10 20 40
```

Supplying the flag runs only full-response pooled-client idle blocks, bracketed
by zero-delay controls. Without it, the existing source-size/compression/close
controls remain. Delays must be finite in [0,100] ms and are validated before
clients initialize. Warm-ups are excluded per block. All complete hits must
match the reference; active routing/UUID/count must remain unchanged. Clients
close after success or failure; no Redis or embedding model is used.

Reports include requested delay, measured `inter_request_gap_ms`, alias timing
and `pair_ms` (the sum of search, gap and alias component timing). Added waiting
is included in that sum: a shorter alias request alone is not an optimization.
Compare both zero-delay brackets and total pair timing, and keep these component
diagnostics separate from HTTP throughput. The client library differs from the
previous curl controls, so establish its own zero-delay baseline rather than
treating results as directly interchangeable.

Five offline tests verify early argument rejection/help, finite bounds, full
response/zero-bracket construction, included waiting, changed-hit rejection
and client cleanup. All 457 tests passed in the full offline run. Actual help
passed. The attempted live run could not initialize: Docker Desktop reported
manually paused. The owned process was stopped, Docker left paused. There are
no new idle measurements or root-cause/performance claims. Resume this command
when stores are available; previous paired-trace results remain prior evidence.
