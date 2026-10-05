"""Analyze header-only lo0 tcpdump output from profile_forwarding host controls.

No HTTP payload is read. Request roles are inferred from the controlled workload
and validated packet sizes, not decoded application content.
"""
import argparse
import json
import re
import statistics
from pathlib import Path

LINE = re.compile(r"^(\d+\.\d+) IP 127\.0\.0\.1\.(\d+) > 127\.0\.0\.1\.(\d+): Flags \[([^]]+)\],.* length (\d+)$")


def analyze(lines, *, samples, warmups):
    if samples < 1 or warmups < 0:
        raise ValueError("Invalid sample counts")
    flows = {}
    for line in lines:
        match = LINE.match(line.strip())
        if not match:
            continue
        timestamp, source, destination, flags, length = match.groups()
        source, destination, length = int(source), int(destination), int(length)
        if (source == 9200) == (destination == 9200):
            continue
        port = destination if source == 9200 else source
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
        if not responses or sum(row[3] for row in responses) != 177 or not acknowledgements:
            raise ValueError("Incomplete alias-sized response or acknowledgement")
        previous = [row for row in rows if row[1] and row[3] and row[0] < request[0]]
        if len(requests) == 2 and sum(row[3] for row in previous) < 5000:
            raise ValueError("Missing preceding large search response")
        groups["reused" if len(requests) == 2 else "fresh"].append({
            "request_time": request[0],
            "request_to_ack_ms": (acknowledgements[0][0] - request[0]) * 1000,
            "request_to_response_ms": (responses[0][0] - request[0]) * 1000,
            "preceding_response_to_request_ms": (request[0] - previous[-1][0]) * 1000 if previous else None,
        })
    report = {"inference": "roles inferred from controlled host curl packet sizes; not decoded HTTP",
              "samples_per_block": samples, "excluded_warmups_per_block": warmups, "blocks": []}
    size = samples + warmups
    for kind, rows in groups.items():
        rows.sort(key=lambda row: row["request_time"])
        if len(rows) != size * 2:
            raise ValueError("Trace does not contain both complete control blocks")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        with args.trace.open() as source:
            report = analyze(source, samples=args.samples, warmups=args.warmups)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Trace validation failed; no attribution claimed."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
