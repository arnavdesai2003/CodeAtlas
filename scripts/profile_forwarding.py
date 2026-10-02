"""Read-only curl search/alias pairs across the local Docker forwarding path.

Run each location separately without other load or writers. curl timings exclude
Python/Docker process startup and are not application HTTP/capacity measurements.
No container files, Elasticsearch settings, corpus data or Redis are changed.
"""

import argparse
import json
import math
import platform
import statistics
import subprocess
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

from app.search import engine
from app.search.indexes import alias_target
from app.core.config import settings
from scripts.benchmark_api import QUERIES, percentile


def curl_output(role):
    fields = [f'"role":"{role}"', '"status":%{http_code}',
              '"dns_s":%{time_namelookup}', '"connect_s":%{time_connect}',
              '"ready_s":%{time_pretransfer}', '"first_byte_s":%{time_starttransfer}',
              '"total_s":%{time_total}', '"bytes":%{size_download}',
              '"connects":%{num_connects}', '"local_port":%{local_port}']
    return "{" + ",".join(fields) + "}\n"


def request_pair(base_url, request, *, index, close, location, container):
    common = ["--silent", "--show-error", "--max-time", "30", "--noproxy", "*",
              "--proto", "=http", "--output", "/dev/null"]
    command = ["curl", *common, "--header", "Content-Type: application/json",
               "--header", "Connection: close" if close else "Connection: keep-alive",
               "--data-binary", json.dumps(request), "--write-out", curl_output("search"),
               f"{base_url}/{index}/_search", "--next", *common,
               "--write-out", curl_output("alias"), f"{base_url}/_alias/{engine.SEARCH_ALIAS}"]
    if location != "host":
        command = ["docker", "exec", container, *command]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=75)
    if completed.returncode:
        raise RuntimeError(f"curl diagnostic failed (exit {completed.returncode}): {completed.stderr.strip()}")
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    if len(rows) != 2 or [row.get("role") for row in rows] != ["search", "alias"]:
        raise RuntimeError("Expected exactly one search and one alias timing record.")
    for row in rows:
        for key in ("dns_s", "connect_s", "ready_s", "first_byte_s", "total_s", "bytes"):
            value = row.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise RuntimeError(f"Invalid curl timing/count: {key}")
        # curl can report a small DNS-cache lookup time but zero connect time
        # for a reused socket; connect time participates only for new sockets.
        valid_phases = row["dns_s"] <= row["ready_s"] <= row["first_byte_s"] <= row["total_s"]
        if row["connects"]:
            valid_phases = valid_phases and row["dns_s"] <= row["connect_s"] <= row["ready_s"]
        if not valid_phases:
            raise RuntimeError(f"curl timing phases are inconsistent: {row}")
    # Reused connections report zero DNS/connect time on the second transfer.
    if rows[0]["connects"] != 1 or rows[1]["connects"] != (1 if close else 0):
        raise RuntimeError("Observed connection lifecycle differs from the requested control.")
    # The container's curl 7.76.1 reports -1 for a reused socket's local port.
    # Its num_connects=0 still verifies reuse; compare ports when both exist.
    if (not close and rows[0]["local_port"] > 0 and rows[1]["local_port"] > 0
            and rows[0]["local_port"] != rows[1]["local_port"]):
        raise RuntimeError("The alias request did not reuse the search socket.")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--location", choices=["host", "container", "container-host-route"], default="host")
    parser.add_argument("--container", default="codeatlas-elasticsearch")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 0:
        parser.error("samples must be positive and warmups nonnegative")
    url = urlsplit(settings.elasticsearch_url)
    if (url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1") or
            url.port != 9200 or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment):
        parser.error("This local diagnostic requires unauthenticated loopback HTTP Elasticsearch on port 9200.")
    base_url = "http://host.docker.internal:9200" if args.location == "container-host-route" else "http://127.0.0.1:9200"
    client = engine.elasticsearch_client
    target = alias_target(client, alias_name=engine.SEARCH_ALIAS)
    index = target or engine.INDEX_NAME
    uuid = client.indices.get_settings(index=index)[index]["settings"]["index"]["uuid"]
    count = client.count(index=index)["count"]
    recorder = Mock()
    recorder.options.return_value = recorder
    recorder.search.return_value = {"hits": {"hits": []}}
    requests = []
    with patch.object(engine, "elasticsearch_client", recorder):
        for query in QUERIES:
            engine.bm25_search(query, 40, index_name=index)
            kwargs = recorder.search.call_args.kwargs
            requests.append({"size": kwargs["size"], "query": kwargs["query"]})
    version_command = ["curl", "--version"]
    if args.location != "host":
        version_command = ["docker", "exec", args.container, *version_command]
    version = subprocess.run(version_command, capture_output=True, text=True, check=True, timeout=15).stdout.splitlines()[0]
    print(json.dumps({"diagnostic": "curl forwarding, not application HTTP",
        "location": args.location, "host_platform": platform.platform(), "curl": version,
        "index": index, "uuid": uuid, "documents": count,
        "samples_per_block": args.samples, "excluded_warmups": args.warmups}), flush=True)
    for label, close in [("reuse_A", False), ("close_A", True), ("reuse_B", False), ("close_B", True)]:
        timings = {role: [] for role in ("search", "alias")}
        for i in range(args.samples + args.warmups):
            rows = request_pair(base_url, requests[i % len(requests)], index=index, close=close,
                                location=args.location, container=args.container)
            if rows[0]["status"] != 200 or rows[1]["status"] != (200 if target else 404):
                raise RuntimeError("Unexpected HTTP status; discard this comparison.")
            if i >= args.warmups:
                for row in rows:
                    timings[row["role"]].append(row)
        for role, rows in timings.items():
            averages = {key.removesuffix("_s") + "_ms": statistics.mean(row[key] * 1000 for row in rows)
                        for key in ("dns_s", "connect_s", "ready_s", "first_byte_s", "total_s")}
            wait = [(row["first_byte_s"] - row["ready_s"]) * 1000 for row in rows]
            print(json.dumps({"block": label, "role": role, "successes": len(rows),
                "averages": averages, "response_wait_avg_ms": statistics.mean(wait),
                "response_wait_p95_ms": percentile(wait, .95),
                "response_bytes_avg": statistics.mean(row["bytes"] for row in rows)}), flush=True)
    if (alias_target(client, alias_name=engine.SEARCH_ALIAS) != target or
            client.count(index=index)["count"] != count or
            client.indices.get_settings(index=index)[index]["settings"]["index"]["uuid"] != uuid):
        raise RuntimeError("Routing/identity/count changed; discard this comparison.")
    print(json.dumps({"completed": True, "routing_identity_count_unchanged": True}), flush=True)


if __name__ == "__main__":
    main()
