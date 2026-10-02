"""Offline checks for diagnostic isolation and measurement attribution."""

import json
import unittest
from unittest.mock import Mock, patch

import httpx

from scripts.benchmark_http_miss_burst import stop_server, validate_response
from scripts.http_miss_burst_server import validate_namespace


class HttpBurstTests(unittest.TestCase):
    def response(self, **changes):
        payload = dict(query="q", limit=10, count=0, results=[], cache_hit=False,
                       search_latency_ms=2.0, elasticsearch_latency_ms=1.0)
        payload.update(changes)
        return httpx.Response(200, content=json.dumps(payload),
                              headers={"X-CodeAtlas-Diagnostic-Worker": "123"})

    def test_namespace_rejects_live_keys_and_patterns(self):
        self.assertEqual(validate_namespace("codeatlas:diagnostic:httpburst:" + "a" * 32 + ":"),
                         "codeatlas:diagnostic:httpburst:" + "a" * 32 + ":")
        for prefix in ("", "codeatlas:search:", "codeatlas:diagnostic:httpburst:*:",
                       "codeatlas:diagnostic:httpburst:" + "a" * 32 + ":extra"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                validate_namespace(prefix)

    def test_legacy_retrieval_and_shared_attribution(self):
        self.assertEqual(validate_response(self.response(), "q", {"123": {}})[1], "retrieved")
        self.assertEqual(validate_response(self.response(cache_coalesced=True,
            elasticsearch_latency_ms=None, coalescing_wait_ms=1), "q", {"123": {}})[1], "coalesced")

    def test_invalid_attribution_and_envelopes_fail(self):
        for changes in (dict(cache_hit=True, cache_coalesced=True),
                        dict(cache_coalesced=True), dict(search_latency_ms=float("nan")),
                        dict(elasticsearch_latency_ms=None), dict(count=1), dict(query="other")):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                validate_response(self.response(**changes), "q", {"123": {}})
        with self.assertRaises(RuntimeError):
            validate_response(self.response(), "q", {"456": {}})

    def test_exited_server_is_reaped(self):
        process = Mock(pid=123)
        with patch("scripts.benchmark_http_miss_burst.os.killpg", side_effect=ProcessLookupError):
            stop_server(process)
        process.wait.assert_called_once_with(timeout=10)
