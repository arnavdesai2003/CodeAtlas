"""Verify Redis cache fencing in a private namespace, without retrieval or models."""
import json
from unittest.mock import patch
from uuid import uuid4

from app.search import cache


def _require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def _clear_namespace(client, prefix):
    batch = []
    for key in client.scan_iter(match=f"{prefix}*", count=128):
        _require(isinstance(key, str) and key.startswith(prefix), "Unexpected cleanup key.")
        batch.append(key)
        if len(batch) == 128:
            client.delete(*batch)
            batch.clear()
    if batch:
        client.delete(*batch)
    _require(not list(client.scan_iter(match=f"{prefix}*", count=128)), "Private namespace cleanup failed.")


def verify_cache_protocol(client, *, on_namespace=None):
    prefix = f"codeatlas:verification:cache:{uuid4().hex}:"
    if on_namespace is not None:
        on_namespace(prefix)
    normal_generation_key = cache.GENERATION_KEY
    normal_generation = client.get(normal_generation_key)
    checks = []
    fresh = [{"name": "fresh"}]
    query = "cache protocol check"
    try:
        with patch.object(cache, "redis_client", client), \
             patch.object(cache, "GENERATION_KEY", prefix + "generation"), \
             patch.object(cache, "ENTRY_PREFIX", prefix + "entries:"):
            old = cache.get_cached_search(query, 10)
            _require(old.results is None and old.generation, "Initial generation-bound miss failed.")
            _require(cache.set_cached_search(query, 10, fresh, generation=old.generation), "Initial fill failed.")
            _require(cache.get_cached_search(query, 10).results == fresh, "Cache read differed from fill.")
            key = cache.build_cache_key(query, 10, generation=old.generation)
            _require(0 < client.pttl(key) <= cache.settings.search_cache_ttl * 1000, "Cache TTL was invalid.")
            checks.append("lua_fill_read_and_ttl")

            for label, corrupt in (
                ("duplicate_json_refill", '[{"name":"one","name":"two"}]'),
                ("nested_json_refill", '[{"nested":' + '[' * cache.MAX_CACHE_NESTING
                 + '0' + ']' * cache.MAX_CACHE_NESTING + '}]'),
            ):
                client.set(key, corrupt, ex=cache.settings.search_cache_ttl)
                lookup = cache.get_cached_search(query, 10)
                _require(lookup.results is None and lookup.generation == old.generation, "Corruption fallback lost fence.")
                _require(cache.set_cached_search(query, 10, fresh, generation=lookup.generation), "Healthy refill failed.")
                _require(cache.get_cached_search(query, 10).results == fresh, "Healthy refill was not retained.")
                checks.append(label)

            cache.invalidate_search_cache(strict=True)
            current = cache.get_cached_search(query, 10)
            _require(current.generation and current.generation != old.generation, "Rotation did not replace generation.")
            _require(cache.set_cached_search(query, 10, fresh, generation=current.generation), "New-generation fill failed.")
            _require(not cache.set_cached_search(query, 10, [{"name": "stale"}], generation=old.generation),
                     "Old-generation fill was accepted.")
            _require(cache.get_cached_search(query, 10).results == fresh, "Stale fill changed fresh entry.")
            checks.append("stale_fill_rejected_after_rotation")

            for token, decoy in (("old*", "old-other"), ("old?", "oldx"),
                                 ("old[ab]", "olda"), ("old\\end", "oldend")):
                client.set(cache.GENERATION_KEY, token)
                old_key = cache.build_cache_key(query, 10, generation=token)
                decoy_key = cache.build_cache_key(query, 10, generation=decoy)
                client.set(old_key, json.dumps(fresh), ex=cache.settings.search_cache_ttl)
                client.set(decoy_key, "decoy", ex=cache.settings.search_cache_ttl)

                class RefillDuringRotation:
                    def __getattr__(self, name):
                        return getattr(client, name)

                    def getset(self, generation_key, replacement):
                        previous = client.getset(generation_key, replacement)
                        new_key = cache.build_cache_key(query, 10, generation=replacement)
                        client.set(new_key, json.dumps(fresh), ex=cache.settings.search_cache_ttl)
                        return previous

                with patch.object(cache, "redis_client", RefillDuringRotation()):
                    deleted = cache.invalidate_search_cache(strict=True)
                _require(deleted == 1 and client.get(old_key) is None, "Old token cleanup was not exact.")
                _require(client.get(decoy_key) == "decoy", "Cleanup deleted a different generation.")
                _require(cache.get_cached_search(query, 10).results == fresh, "Cleanup deleted a post-rotation refill.")
            checks.append("literal_globs_and_post_rotation_refill")

            client.set(cache.GENERATION_KEY, "")
            lookup = cache.get_cached_search(query, 10)
            _require(lookup.results is None and lookup.generation is None, "Empty token did not disable caching.")
            _require(not cache.set_cached_search(query, 10, fresh, generation=""), "Empty token allowed a fill.")
            cache.invalidate_search_cache(strict=True)
            _require(cache.get_cached_search(query, 10).generation, "Rotation did not recover empty metadata.")
            checks.append("empty_generation_fallback_and_recovery")
    finally:
        _clear_namespace(client, prefix)
        _require(client.get(normal_generation_key) == normal_generation, "Normal cache generation changed during verification.")
    return {"status": "passed", "checks": checks, "namespace_removed": True,
            "normal_generation_unchanged": True, "retrieval_executed": False}


def main():
    try:
        report = verify_cache_protocol(cache.redis_client, on_namespace=lambda prefix: print(
            json.dumps({"status": "starting", "private_namespace": prefix}), flush=True,
        ))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Protocol verification or private cleanup failed; no success claimed."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
