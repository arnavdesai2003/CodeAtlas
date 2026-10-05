"""Header diagnostics must reject incomplete/mismatched control traces."""
import unittest
from scripts.analyze_forwarding_headers import analyze


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
