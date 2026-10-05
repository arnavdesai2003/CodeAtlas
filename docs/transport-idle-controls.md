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

## Completed live controls after resume

After the user resumed Docker, PostgreSQL/Elasticsearch/Redis were healthy.
Two read-only runs used 20 measured pairs and five excluded warm-ups per block.
Run A requested delays 0/1/5/10/20/40 ms; run B reversed the nonzero order.
Each had seven blocks with zero-delay A/B brackets: 140 measured pairs per run,
280 total, plus 70 excluded warm-up pairs. Ten reference searches per run were
also unmeasured. All complete hits matched each run's reference; both runs
completed with unchanged legacy routing/UUID/4,340-document count.

Machine: macOS 27.0.1 arm64, Python 3.13.15, Elasticsearch Python client 9.5.0,
elastic-transport 9.4.2, urllib3 2.7.0; Elasticsearch server 9.4.3. Plain pooled
client, identity encoding, average full decoded response approximately 75,293
bytes. No embedding model or Redis operations occurred. No writers/load were
launched by the experiment; unrelated host scheduling/load was not controlled.

| Control | A actual gap avg ms | A alias avg ms | A pair sum avg ms | B actual gap avg ms | B alias avg ms | B pair sum avg ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Zero bracket A | .001 | 16.929 | 20.870 | .001 | 15.046 | 18.078 |
| Requested 1 ms | 1.259 | 13.936 | 18.536 | 1.230 | 13.425 | 17.575 |
| Requested 5 ms | 6.124 | 7.946 | 17.426 | 6.042 | 7.730 | 16.010 |
| Requested 10 ms | 12.220 | 4.768 | 20.357 | 12.046 | 2.002 | 16.800 |
| Requested 20 ms | 23.613 | 1.569 | 28.614 | 23.982 | 2.504 | 32.350 |
| Requested 40 ms | 43.494 | 3.078 | 53.198 | 44.137 | 3.388 | 53.884 |
| Zero bracket B | .001 | 21.429 | 26.969 | .001 | 14.049 | 16.904 |

Waiting before the request reduces its subsequent latency in both orders,
consistent with connection/forwarding state changing during the interval. This
does not identify a specific timer, buffering policy, kernel mechanism or delay
leg. At 20/40 ms the total pair cost worsens despite much faster alias requests.
Shorter gaps sometimes improve pair means, but zero controls and search times
drift materially, and these small sequential samples are not application HTTP
evidence. Sleep overshoot is measured, not replaced by its requested value.
No production delay or setting change is justified by this diagnostic alone.

The prior 457-test offline pass is inherited, not rerun for this documentation-only
live checkpoint. No retrieval/ranking code changed, so quality evaluation was not
rerun. No corpus rebuild, alias switch, cache flush/rotation, `.env`, process/device
default or existing API change occurred. No new throughput/capacity claim.
