"""Read-only response-size/connection controls, not HTTP or capacity results.

Source-free and zero-hit requests are diagnostic controls only. Production
search keeps all result fields, ranking and per-request generation resolution.
Run without concurrent indexing/publication; uses no Redis or embedding model.
"""

import argparse
import json
import platform
import statistics
import math
from time import perf_counter, sleep
from unittest.mock import Mock, patch

from elasticsearch import Elasticsearch
from elastic_transport import Urllib3HttpNode

from app.core.config import settings
from app.search import engine
from app.search.indexes import resolve_active_index
from scripts.benchmark_api import QUERIES, percentile


class TracingNode(Urllib3HttpNode):
    def perform_request(self, *args, **kwargs):
        response = super().perform_request(*args, **kwargs)
        self.last_response = {
            "decoded_bytes": len(response.body),
            "node_ms": response.meta.duration * 1000,
            "content_encoding": response.meta.headers.get("content-encoding"),
        }
        return response


def idle_delay(value):
    try:
        delay = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("idle delay must be finite and in [0, 100] ms") from exc
    if not math.isfinite(delay) or not 0 <= delay <= 100:
        raise argparse.ArgumentTypeError("idle delay must be finite and in [0, 100] ms")
    return delay


def idle_controls(delays, client):
    return [("idle_0_A", client, {}, 0)] + [
        (f"idle_{delay:g}", client, {}, delay) for delay in delays if delay > 0
    ] + [("idle_0_B", client, {}, 0)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--idle-delays-ms", nargs="+", type=idle_delay,
                        help="Run only full-response idle controls, bracketed by zero-delay blocks; added wait is included in pair timing.")
    args = parser.parse_args(argv)
    if args.samples < 1 or args.warmups < 0:
        parser.error("samples must be positive and warmups nonnegative")
    clients = []
    try:
        for compressed in (False, True):
            clients.append(Elasticsearch(settings.elasticsearch_url, http_compress=compressed,
                node_class=TracingNode, request_timeout=60, retry_on_timeout=True, max_retries=3))
        plain, gzip = clients
        resolve = lambda client: resolve_active_index(client, alias_name=engine.SEARCH_ALIAS,
                                                       legacy_name=engine.INDEX_NAME)
        index = resolve(plain)
        uuid = plain.indices.get_settings(index=index)[index]["settings"]["index"]["uuid"]
        count = plain.count(index=index)["count"]
        # Capture the current production BM25 request rather than duplicate its
        # query builder. The mock exists only in this standalone process/scope.
        requests = {}
        recorder = Mock()
        recorder.options.return_value = recorder
        recorder.search.return_value = {"hits": {"hits": []}}
        with patch.object(engine, "elasticsearch_client", recorder):
            for query in QUERIES:
                engine.bm25_search(query, 40, index_name=index)
                requests[query] = recorder.search.call_args.kwargs
        expected = {query: plain.search(**request)["hits"] for query, request in requests.items()}
        print(json.dumps({"diagnostic": "search response/connection controls",
            "platform": platform.platform(), "python": platform.python_version(),
            "index": index, "uuid": uuid, "documents": count,
            "samples_per_block": args.samples, "excluded_warmups": args.warmups}), flush=True)
        controls = [
            ("full", plain, {}), ("no_source", plain, {"source": False}),
            ("no_embedding", plain, {"source": {"excludes": ["embedding"]}}),
            ("zero_hits", plain, {"size": 0}), ("gzip", gzip, {}),
            ("close_search", plain.options(headers={"connection": "close"}), {}),
            ("full_control", plain, {}),
        ]
        controls = idle_controls(args.idle_delays_ms, plain) if args.idle_delays_ms is not None else [
            (*control, 0) for control in controls]
        for label, client, changes, delay in controls:
            timings = {key: [] for key in ("search_ms", "alias_ms", "node_ms", "took_ms", "decoded_bytes", "inter_request_gap_ms", "pair_ms")}
            encodings = set()
            for i in range(args.warmups + args.samples):
                query = QUERIES[i % len(QUERIES)]
                node = client.transport.node_pool.get()
                start = perf_counter()
                response = client.search(**(requests[query] | changes))
                search_ms = (perf_counter() - start) * 1000
                search_end = perf_counter()
                trace = node.last_response.copy()
                if delay:
                    sleep(delay / 1000)
                start = perf_counter()
                gap_ms = (start - search_end) * 1000
                observed = resolve(gzip if label == "gzip" else plain)
                alias_ms = (perf_counter() - start) * 1000
                if observed != index:
                    raise RuntimeError("Routing changed; discard this comparison.")
                if not changes and response["hits"] != expected[query]:
                    raise RuntimeError("Control changed search hits; discard this comparison.")
                if i >= args.warmups:
                    for key, value in {"search_ms": search_ms, "alias_ms": alias_ms,
                        "node_ms": trace["node_ms"], "took_ms": response["took"],
                        "decoded_bytes": trace["decoded_bytes"], "inter_request_gap_ms": gap_ms,
                        "pair_ms": search_ms + gap_ms + alias_ms}.items():
                        timings[key].append(value)
                    encodings.add(trace["content_encoding"] or "identity")
            print(json.dumps({"control": label, "successes": args.samples,
                "requested_idle_ms": delay,
                "averages": {key: statistics.mean(values) for key, values in timings.items()},
                "alias_p95_ms": percentile(timings["alias_ms"], .95),
                "response_encodings": sorted(encodings)}), flush=True)
        if (resolve(plain) != index or plain.count(index=index)["count"] != count or
                plain.indices.get_settings(index=index)[index]["settings"]["index"]["uuid"] != uuid):
            raise RuntimeError("Routing/identity/count changed; discard this comparison.")
        print(json.dumps({"completed": True, "routing_identity_count_unchanged": True}), flush=True)
    finally:
        for client in clients:
            client.close()


if __name__ == "__main__":
    main()
