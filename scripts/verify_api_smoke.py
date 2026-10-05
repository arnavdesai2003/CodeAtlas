"""Exercise production API routes with live stores and isolated cache keys.

Uses ASGI transport without application lifespan; no schema initialization,
repository mutation, listening server or existing API process is involved.
"""
import argparse
import json
from unittest.mock import patch
from uuid import uuid4


def verify_api_smoke():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.config import settings
    from app.search import cache
    from scripts.verify_cache_protocol import _clear_namespace

    client = cache.redis_client
    normal_key = cache.GENERATION_KEY
    normal_generation = client.get(normal_key)
    prefix = "codeatlas:verification:api:" + uuid4().hex + ":"
    print(json.dumps({"status": "starting", "private_namespace": prefix}), flush=True)
    headers = {}
    key = settings.api_key.get_secret_value()
    if key:
        headers["X-CodeAtlas-API-Key"] = key
    checks = []
    try:
        with patch.object(cache, "CACHE_PREFIX", prefix), \
             patch.object(cache, "ENTRY_PREFIX", prefix + "entries:"), \
             patch.object(cache, "GENERATION_KEY", prefix + "generation"):
            # Deliberately no TestClient context manager: lifespan creates tables.
            http = TestClient(app, headers=headers)
            try:
                health = http.get("/health")
                if health.status_code != 200 or health.json().get("status") != "healthy":
                    raise RuntimeError("Dependencies are not healthy")
                repositories = http.get("/repositories")
                if repositories.status_code != 200 or repositories.json().get("count", 0) < 1:
                    raise RuntimeError("Repository listing failed")
                checks.append("healthy_dependencies_and_repository_listing")
                payload = {"query": "compute gradients using reverse mode automatic differentiation", "limit": 10}
                responses = [http.post("/search", json=payload) for _ in range(2)]
                if any(response.status_code != 200 for response in responses):
                    raise RuntimeError("Search failed")
                first, second = [response.json() for response in responses]
                if (first.get("cache_hit") is not False or second.get("cache_hit") is not True
                        or first.get("cache_coalesced") or second.get("cache_coalesced")
                        or not first.get("results") or first["results"] != second.get("results")
                        or first.get("count") != len(first["results"]) or first["count"] > 10):
                    raise RuntimeError("Cold/warm search contract failed")
                checks.append("isolated_cold_search_and_identical_cache_hit")
                for invalid in ({"query": " "}, {"query": "example", "limit": 0},
                                {"query": "example", "limit": 101}):
                    if http.post("/search", json=invalid).status_code != 422:
                        raise RuntimeError("Invalid search was not rejected")
                checks.append("query_and_limit_validation")
                if key and http.get("/repositories", headers={"X-CodeAtlas-API-Key": ""}).status_code != 401:
                    raise RuntimeError("Missing API key was not rejected")
                if key:
                    checks.append("configured_api_key_required")
            finally:
                http.close()
    finally:
        _clear_namespace(client, prefix)
        if client.get(normal_key) != normal_generation:
            raise RuntimeError("Normal cache generation changed")
    return {"status": "passed", "checks": checks, "private_keys_removed": True,
            "normal_cache_generation_unchanged": True, "schema_initialized": False,
            "transport": "in-process ASGI with live stores"}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        report = verify_api_smoke()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "API smoke or private cleanup failed; no success claimed."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
