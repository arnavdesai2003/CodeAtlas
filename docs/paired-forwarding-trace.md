# Paired host/container forwarding evidence — 2026-10-05

This analyzes the simultaneous capture run requested by the user, rather than
claiming a new workload run. Host lo0 and container eth0 header traces cover the
same unchanged `profile_forwarding --location host --samples 20 --warmups 5`
workload. All 80 measured pairs completed with expected 200 search/404 absent
alias statuses; 20 warm-up pairs were excluded. Both captures reported zero
kernel drops, stopped normally, and the temporary Alpine/tcpdump helper was
removed. No production container package or setting changed. Raw temporary
traces remain outside Git and contain packet header summaries, not payload dumps.

## Results

| Block | Host alias request→response avg ms | Container alias request→response avg ms | Host previous response→request avg ms | Container previous response→request avg ms |
| --- | ---: | ---: | ---: | ---: |
| Reuse A | 12.726 | .438 | .083 | 12.523 |
| Reuse B | 14.273 | .485 | .091 | 13.943 |
| Fresh A | .745 | .283 | — | — |
| Fresh B | .847 | .289 | — | — |

At the host, the next request follows the previous search response promptly.
At the container, the gap from its last search response data to the next request
is much longer. Once the alias-shaped request reaches container eth0, response
data follows within roughly half a millisecond. This supports localization of
the dominant delay across the forwarding boundary between successive requests,
rather than a comparable delay processing that request inside Elasticsearch.
The inter-request gap includes delivery of the prior response towards the host
and the next request towards the container; it does not separate those legs or
identify a particular buffering/TCP/kernel mechanism. Host and VM clock epochs
are never directly subtracted. This is component evidence, not application HTTP
capacity, a sustained benchmark or evidence for changing transport defaults.

The workload's start/end routing, legacy UUID and 4,340-document count stayed
unchanged. Earlier quality evaluation remains applicable; this diagnostic
changed no retrieval/ranking/indexing code and did not rerun model evaluation.

## Repeatable analysis

```sh
.venv/bin/python -B -m scripts.analyze_forwarding_headers \
  /private/tmp/codeatlas-host-headers.log \
  --container-trace /private/tmp/codeatlas-container-headers.log \
  --samples 20 --warmups 5
```

The analyzer supports the Docker Linux cooked eth0 header layout, including
different whitespace in In/Out records. It requires both complete blocks per
mode, matches all ordered host/container flow signatures (including warm-ups),
then excludes warm-ups from means. Signatures use ordered client payload byte
counts and total preceding/current server response bytes. No HTTP is decoded;
role and alignment are inferred from this serial controlled workload, not unique
request IDs. Equal-sized unrelated flows cannot be distinguished; isolate the
workload and check both capture drop counters. Different header sizes,
retransmissions, segmentation or port reuse can cause validation failure.
The container can acknowledge through response data instead of a separate ACK.

Four added regressions cover independent clock offsets, piggyback acknowledgement,
size/completeness mismatch and per-block warm-up exclusion. An initial synthetic
clock-offset fixture generated negative epoch timestamps; corrected the fixture
to keep both clocks positive while testing offsets in either direction.
The final full offline run passed all 452 tests. Existing host-only analysis
also reproduced its original output on the retained trace.

Further attribution requires forwarding instrumentation or controlled changes
that distinguish previous-response delivery from next-request forwarding. No
kernel, Docker or application defaults were changed by this milestone.
