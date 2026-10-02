"""Explicit, loopback-only benchmark credentials; never load server secrets."""
import os
from ipaddress import ip_address
from urllib.parse import urlsplit


def benchmark_headers(base_url: str) -> dict[str, str]:
    key = os.environ.get("CODEATLAS_BENCHMARK_API_KEY", "")
    if not key:
        return {}
    target = urlsplit(base_url)
    try:
        loopback = target.hostname == "localhost" or ip_address(target.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (target.scheme not in {"http", "https"} or not loopback or
            target.username is not None or target.password is not None):
        raise ValueError("Keyed benchmarks require a loopback URL without credentials.")
    if not key.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in key):
        raise ValueError("Benchmark API key must contain printable ASCII characters without whitespace.")
    return {"X-CodeAtlas-API-Key": key}
