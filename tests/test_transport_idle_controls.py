"""Idle experiments must count added waiting and preserve complete search hits."""
import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch
from scripts import profile_search_transport as command


class TransportIdleControlTests(unittest.TestCase):
    def test_invalid_delays_and_help_precede_clients(self):
        with patch.object(command, "Elasticsearch") as clients:
            for argv, status in ((["--help"], 0), (["--idle-delays-ms", "nan"], 2),
                                 (["--idle-delays-ms", "-1"], 2), (["--idle-delays-ms", "101"], 2),
                                 (["--idle-delays-ms", "inf"], 2)):
                with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        command.main(argv)
                    self.assertEqual(result.exception.code, status)
            clients.assert_not_called()

    def test_controls_keep_client_and_full_response_between_zero_brackets(self):
        client = object()
        controls = command.idle_controls([0, 1, 20], client)
        self.assertEqual([row[3] for row in controls], [0, 1, 20, 0])
        self.assertTrue(all(row[1] is client and row[2] == {} for row in controls))

    def fixture(self):
        client = Mock()
        client.indices.get_settings.return_value = {"symbols": {"settings": {"index": {"uuid": "uuid"}}}}
        client.count.return_value = {"count": 1}
        client.search.return_value = {"hits": {"hits": []}, "took": 1}
        node = Mock(last_response={"decoded_bytes": 42, "node_ms": .5, "content_encoding": None})
        client.transport.node_pool.get.return_value = node
        return client

    def run_probe(self, client, output):
        clock = [0]
        def time():
            clock[0] += .001
            return clock[0]
        def pause(seconds):
            clock[0] += seconds
        with patch.object(command, "Elasticsearch", return_value=client), \
             patch.object(command, "resolve_active_index", return_value="symbols"), \
             patch.object(command, "QUERIES", ["example"]), \
             patch.object(command, "perf_counter", side_effect=time), \
             patch.object(command, "sleep", side_effect=pause) as sleep, contextlib.redirect_stdout(output):
            command.main(["--samples", "1", "--warmups", "0", "--idle-delays-ms", "20"])
        return sleep

    def test_added_wait_is_in_gap_and_total_pair_not_alias_timing(self):
        output = io.StringIO()
        client = self.fixture()
        sleep = self.run_probe(client, output)
        sleep.assert_called_once_with(.020)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        block = next(row for row in rows if row.get("control") == "idle_20")
        averages = block["averages"]
        self.assertGreaterEqual(averages["inter_request_gap_ms"], 20)
        self.assertAlmostEqual(averages["pair_ms"], averages["search_ms"] + averages["inter_request_gap_ms"] + averages["alias_ms"])
        self.assertAlmostEqual(averages["alias_ms"], 1)
        self.assertTrue(rows[-1]["completed"])
        self.assertEqual(client.close.call_count, 2)

    def test_changed_complete_hits_fail_and_close_clients(self):
        client = self.fixture()
        client.search.side_effect = [{"hits": {"hits": []}, "took": 1},
                                     {"hits": {"hits": [{"_id": "different"}]}, "took": 1}]
        output = io.StringIO()
        with self.assertRaisesRegex(RuntimeError, "changed search hits"):
            self.run_probe(client, output)
        self.assertNotIn('"completed": true', output.getvalue())
        self.assertEqual(client.close.call_count, 2)

    def test_threshold_endpoints_are_finite_and_valid(self):
        self.assertEqual(command.idle_delay("0"), 0)
        self.assertEqual(command.idle_delay("100"), 100)
