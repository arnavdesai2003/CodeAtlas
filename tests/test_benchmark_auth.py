import unittest
from unittest.mock import patch
import httpx

from scripts.http_auth import benchmark_headers
from scripts.benchmark_api import run_request


class BenchmarkAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_keyless_client_does_not_load_server_key(self):
        with patch.dict("os.environ", {"API_KEY": "server-secret", "CODEATLAS_BENCHMARK_API_KEY": ""}):
            self.assertEqual(benchmark_headers("http://example.com"), {})

    def test_keyed_targets_and_header_validation(self):
        with patch.dict("os.environ", {"CODEATLAS_BENCHMARK_API_KEY": "client-secret"}):
            for target in ("http://127.0.0.1:8001", "http://localhost:8000", "http://[::1]:8001"):
                self.assertEqual(benchmark_headers(target), {"X-CodeAtlas-API-Key": "client-secret"})
            for target in ("http://example.com", "https://example.com", "http://user@localhost", "file:///tmp"):
                with self.assertRaises(ValueError):
                    benchmark_headers(target)
        for key in ("secret\nheader", " secret", "secret ", "sécret"):
            with patch.dict("os.environ", {"CODEATLAS_BENCHMARK_API_KEY": key}):
                with self.assertRaises(ValueError) as caught:
                    benchmark_headers("http://localhost")
                self.assertNotIn(key, str(caught.exception))

    async def test_bypass_and_api_key_headers_survive_client_merge(self):
        seen = []
        def handler(request):
            seen.append(request.headers)
            return httpx.Response(503)
        async with httpx.AsyncClient(base_url="http://127.0.0.1", headers={"X-CodeAtlas-API-Key": "client-secret"},
                                     transport=httpx.MockTransport(handler)) as client:
            await run_request(client, "q", uncached=True)
        self.assertEqual(seen[0]["X-CodeAtlas-API-Key"], "client-secret")
        self.assertEqual(seen[0]["X-CodeAtlas-Benchmark-Bypass"], "true")
