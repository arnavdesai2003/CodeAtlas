"""Analyze host lo0 headers and optional paired container eth0 headers.

No HTTP payload is read. Request roles are inferred from the controlled workload
and validated packet sizes, not decoded application content.
"""
import argparse
import json
import re
import statistics
from pathlib import Path

LINE = re.compile(r"^(\d+\.\d+) IP 127\.0\.0\.1\.(\d+) > 127\.0\.0\.1\.(\d+): Flags \[([^]]+)\],.* length (\d+)$")
CONTAINER_LINE = re.compile(r"^(\d+\.\d+) eth0\s+(?:In|Out)\s+IP ([\d.]+)\.(\d+) > ([\d.]+)\.(\d+): Flags \[([^]]+)\],.* length (\d+)$")


def collect_flows(lines, *, samples, warmups, container=False):
    if samples < 1 or warmups < 0:
        raise ValueError("Invalid sample counts")
    flows = {}
    for line in lines:
        match = (CONTAINER_LINE if container else LINE).match(line.strip())
        if not match:
            continue
        if container:
            timestamp, source_address, source, destination_address, destination, flags, length = match.groups()
        else:
            timestamp, source, destination, flags, length = match.groups()
            source_address = destination_address = "127.0.0.1"
        source, destination, length = int(source), int(destination), int(length)
        if (source == 9200) == (destination == 9200):
            continue
        port = (destination_address, destination) if source == 9200 else (source_address, source)
        row = (float(timestamp), source == 9200, flags, length)
        flows.setdefault(port, []).append(row)
    groups = {"reused": [], "fresh": []}
    for rows in flows.values():
        requests = [row for row in rows if not row[1] and row[3]]
        if not requests or requests[-1][3] != 108:
            continue
        if len(requests) not in (1, 2) or any(not row[1] and "S" in row[2] for row in rows[1:]):
            raise ValueError("Unexpected connection request lifecycle")
        request = requests[-1]
        responses = [row for row in rows if row[1] and row[3] and row[0] >= request[0]]
        acknowledgements = [row for row in rows if row[1] and not row[3]
                            and row[0] >= request[0] and "." in row[2] and "F" not in row[2]]
        if not responses or sum(row[3] for row in responses) != 177 or (not container and not acknowledgements):
            raise ValueError("Incomplete alias-sized response or acknowledgement")
        previous = [row for row in rows if row[1] and row[3] and row[0] < request[0]]
        if len(requests) == 2 and sum(row[3] for row in previous) < 5000:
            raise ValueError("Missing preceding large search response")
        groups["reused" if len(requests) == 2 else "fresh"].append({
            "request_time": request[0],
            "request_to_ack_ms": (acknowledgements[0][0] - request[0]) * 1000 if not container else None,
            "request_to_response_ms": (responses[0][0] - request[0]) * 1000,
            "preceding_response_to_request_ms": (request[0] - previous[-1][0]) * 1000 if previous else None,
            "signature": (tuple(row[3] for row in requests), sum(row[3] for row in previous), sum(row[3] for row in responses)),
        })
    size = samples + warmups
    for rows in groups.values():
        rows.sort(key=lambda row: row["request_time"])
        if len(rows) != size * 2:
            raise ValueError("Trace does not contain both complete control blocks")
    return groups


def analyze(lines, *, samples, warmups):
    groups = collect_flows(lines, samples=samples, warmups=warmups)
    report = {"inference": "roles inferred from controlled host curl packet sizes; not decoded HTTP",
              "samples_per_block": samples, "excluded_warmups_per_block": warmups, "blocks": []}
    size = samples + warmups
    for kind, rows in groups.items():
        for block in range(2):
            measured = rows[block * size + warmups:(block + 1) * size]
            values = {key: statistics.mean(row[key] for row in measured)
                      for key in ("request_to_ack_ms", "request_to_response_ms")}
            if kind == "reused":
                values["preceding_response_to_request_ms"] = statistics.mean(
                    row["preceding_response_to_request_ms"] for row in measured)
            report["blocks"].append({"connection": kind, "block": block + 1,
                                     "measured_flows": len(measured), "averages": values})
    return report


def analyze_pair(host_lines, container_lines, *, samples, warmups):
    host = collect_flows(host_lines, samples=samples, warmups=warmups)
    container = collect_flows(container_lines, samples=samples, warmups=warmups, container=True)
    report = {"inference": "ordered controlled flows matched by request/response byte counts, not decoded HTTP",
              "clock_comparison": "within-trace intervals only; no cross-clock timestamps subtracted",
              "samples_per_block": samples, "excluded_warmups_per_block": warmups, "blocks": []}
    size = samples + warmups
    for kind in host:
        pairs = list(zip(host[kind], container[kind], strict=True))
        if any(left["signature"] != right["signature"] for left, right in pairs):
            raise ValueError("Host/container ordered flow signatures differ")
        for block in range(2):
            measured = pairs[block * size + warmups:(block + 1) * size]
            averages = {"host_request_to_response_ms": statistics.mean(left["request_to_response_ms"] for left, _ in measured),
                        "container_request_to_response_ms": statistics.mean(right["request_to_response_ms"] for _, right in measured)}
            if kind == "reused":
                averages.update({
                    "host_previous_response_to_request_ms": statistics.mean(left["preceding_response_to_request_ms"] for left, _ in measured),
                    "container_previous_response_to_request_ms": statistics.mean(right["preceding_response_to_request_ms"] for _, right in measured)})
            report["blocks"].append({"connection": kind, "block": block + 1,
                                     "matched_measured_flows": len(measured), "averages": averages})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--container-trace", type=Path, help="Matching eth0 header trace; compare within-trace intervals.")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        with args.trace.open() as source:
            if args.container_trace:
                with args.container_trace.open() as container:
                    report = analyze_pair(source, container, samples=args.samples, warmups=args.warmups)
            else:
                report = analyze(source, samples=args.samples, warmups=args.warmups)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Trace validation failed; no attribution claimed."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
