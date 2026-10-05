# Host forwarding header trace — 2026-10-05

Authorized interface enumeration and loopback capture succeeded. Capture saved
TCP header summaries only, with a 96-byte snapshot and no hex/ASCII payload output
or packet-file storage. No password was saved to a file. The owned capture stopped
normally: 3,885 packets captured, zero kernel drops. Raw traces are not committed.

Workload: `profile_forwarding --location host --samples 20 --warmups 5`, unchanged
ten BM25 queries/40 candidates/full response (~75,293 bytes average), four
reuse/close blocks. All 80 measured pairs succeeded; 20 warm-up pairs excluded.
Search returned 200, absent alias 404. Routing/UUID/4,340-document count unchanged.
This is curl/packet component evidence, not application HTTP or capacity.

| Block | curl alias response wait avg ms | Packet request → ACK avg ms | Packet request → response data avg ms | Last search response → alias request avg ms |
| --- | ---: | ---: | ---: | ---: |
| Reuse A | 19.646 | .011 | 19.596 | .087 |
| Close A | .931 | .013 | .899 | — |
| Reuse B | 18.748 | .010 | 18.689 | .092 |
| Close B | .799 | .010 | .770 | — |

The second request leaves loopback promptly after search data and is acknowledged
promptly. Response delay follows that ACK. This excludes a comparable client-side
wait before transmission on this captured host TCP leg. A loopback ACK does not
prove delivery to Elasticsearch inside the VM. Forwarding, server processing
and the return path cannot be separated by this single vantage point. No unique
TCP/Docker/kernel mechanism or universal fix is established.

The Elasticsearch container has no tcpdump, tshark or ss available. No package,
kernel or Docker setting changed. A simultaneous VM/container-side or forwarding/
server trace remains the next attribution step. Optional connection-close remains
false by default; this evidence does not override its concurrency tradeoff.

## Reproduction and limitations

Start capture before the host diagnostic; stop it afterwards and check drops.
Authorization/authentication is handled interactively, never in a saved command:

```sh
sudo tcpdump -i lo0 -nn -tt -S -l -s 96 'tcp port 9200' > /private/tmp/codeatlas-forwarding-packet-headers.log
.venv/bin/python -B -m scripts.analyze_forwarding_headers /private/tmp/codeatlas-forwarding-packet-headers.log --samples 20 --warmups 5
```

The narrow parser uses IPv4 loopback connections from this controlled curl
workload: alias-shaped 108-byte requests/177-byte responses, with a large preceding
search response for reused connections. Roles are inferred from packet sizes;
HTTP content is not decoded. Both complete blocks per mode, ACKs and responses
are required. Warm-ups are excluded per block. Client SYN port reuse, extra
requests, missing blocks or unexpected response sizes fail rather than guess.
IPv6 metadata probes are ignored. Retransmissions/segmented requests or different
headers may fail; this is not a general packet decoder. Capture completeness
also requires the operator's drop-counter check.

Initial analyzer validation rejected a normal server SYN-ACK; corrected its
reuse check to inspect subsequent client SYNs and reran successfully. Four
offline regressions cover handshake, control completeness, missing ACKs and
warm-up exclusion. No production retrieval/settings changed.
