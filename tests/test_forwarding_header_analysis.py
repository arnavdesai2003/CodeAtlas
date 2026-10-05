"""Header diagnostics must reject incomplete/mismatched control traces."""
import unittest
from scripts.analyze_forwarding_headers import analyze, analyze_pair


def flow(port, start, reused):
    def packet(offset, server, flags, size):
        source, destination = (9200, port) if server else (port, 9200)
        return f"{start + offset:.6f} IP 127.0.0.1.{source} > 127.0.0.1.{destination}: Flags [{flags}], seq 1, length {size}"
    rows = [packet(0, False, "S", 0), packet(.00001, True, "S.", 0)]
    if reused:
        rows += [packet(.001, False, "P.", 435), packet(.002, True, "P.", 8000)]
    rows += [packet(.003, False, "P.", 108), packet(.00301, True, ".", 0),
             packet(.023 if reused else .004, True, "P.", 177)]
    return rows


class ForwardingHeaderAnalysisTests(unittest.TestCase):
    def fixture(self):
        return sum((flow(50000 + i, i, reused) for i, reused in enumerate((True, False, True, False))), [])

    def test_control_blocks_and_server_syn_ack_preserve_timing(self):
        report = analyze(self.fixture(), samples=1, warmups=0)
        self.assertEqual(len(report["blocks"]), 4)
        for block in report["blocks"]:
            values = block["averages"]
            self.assertAlmostEqual(values["request_to_ack_ms"], .01)
            self.assertAlmostEqual(values["request_to_response_ms"], 20 if block["connection"] == "reused" else 1)

    def test_missing_response_or_control_block_fails(self):
        for lines in (self.fixture()[:-1], self.fixture()[:-5], []):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                analyze(lines, samples=1, warmups=0)

    def test_warmups_are_excluded_in_each_block(self):
        rows = []
        for i, reused in enumerate((True, True, False, False, True, True, False, False)):
            rows.extend(flow(51000 + i, i, reused))
        result = analyze(rows, samples=1, warmups=1)
        self.assertTrue(all(block["measured_flows"] == 1 for block in result["blocks"]))

    def test_missing_ack_or_invalid_sample_counts_fail(self):
        lines = [line for line in self.fixture() if "Flags [.]" not in line]
        for trace, samples, warmups in ((lines, 1, 0), (self.fixture(), 0, 0), (self.fixture(), 1, -1)):
            with self.subTest(samples=samples, warmups=warmups), self.assertRaises(ValueError):
                analyze(trace, samples=samples, warmups=warmups)

    def container(self, rows, *, offset=1000):
        result = []
        for line in rows:
            stamp, text = line.split(" ", 1)
            text = text.replace("IP ", "eth0  In  IP ").replace("127.0.0.1", "172.18.0.1")
            text = text.replace("172.18.0.1.9200", "172.18.0.4.9200")
            result.append(f"{float(stamp) + offset:.6f} {text}")
        return result

    def test_pair_comparison_does_not_subtract_cross_clock_timestamps(self):
        host = [f"{float(line.split(' ', 1)[0]) + 10000:.6f} {line.split(' ', 1)[1]}"
                for line in self.fixture()]
        for offset in (1000, -1000):
            report = analyze_pair(host, self.container(host, offset=offset), samples=1, warmups=0)
            for block in report["blocks"]:
                values = block["averages"]
                self.assertAlmostEqual(values["host_request_to_response_ms"], values["container_request_to_response_ms"])
                self.assertEqual(block["matched_measured_flows"], 1)

    def test_container_can_piggyback_ack_in_response(self):
        rows = [line for line in self.fixture() if "Flags [.]" not in line]
        report = analyze_pair(self.fixture(), self.container(rows), samples=1, warmups=0)
        self.assertEqual(len(report["blocks"]), 4)

    def test_pair_rejects_different_ordered_flow_sizes_or_incomplete_trace(self):
        different = self.container(self.fixture())
        different = [line.replace("length 8000", "length 8001") for line in different]
        for rows in (different, self.container(self.fixture())[:-1]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                analyze_pair(self.fixture(), rows, samples=1, warmups=0)

    def test_pair_excludes_warmups_per_block_after_matching_them(self):
        rows = []
        for i, reused in enumerate((True, True, False, False, True, True, False, False)):
            rows.extend(flow(51000 + i, i, reused))
        report = analyze_pair(rows, self.container(rows), samples=1, warmups=1)
        self.assertTrue(all(block["matched_measured_flows"] == 1 for block in report["blocks"]))
