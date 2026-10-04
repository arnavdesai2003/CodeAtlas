import hashlib
import json
import math
from dataclasses import dataclass
from uuid import uuid4

from app.core.clients import redis_client
from app.core.config import settings


CACHE_PREFIX = "codeatlas:search:"
ENTRY_PREFIX = f"{CACHE_PREFIX}v2:"
GENERATION_KEY = f"{CACHE_PREFIX}generation:v2"
MAX_CACHE_NESTING = 16

# A single Redis operation binds the read to its generation. A random token
# avoids reusing an old generation after metadata eviction or Redis reset.
_READ_SCRIPT = """
local generation = redis.call('GET', KEYS[1])
if not generation then
    generation = ARGV[1]
    redis.call('SET', KEYS[1], generation)
end
local value = redis.call('GET', ARGV[2] .. generation .. ':' .. ARGV[3])
return {generation, value}
"""

# Compare and write atomically: invalidation cannot slip between these steps.
_WRITE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('SETEX', KEYS[2], ARGV[2], ARGV[3])
return 1
"""


@dataclass(frozen=True)
class CacheLookup:
    results: list[dict] | None = None
    generation: str | None = None


def _query_digest(query: str, limit: int) -> str:
    normalized_query = " ".join(query.lower().strip().split())
    return hashlib.sha256(f"{normalized_query}:{limit}".encode("utf-8")).hexdigest()


def build_cache_key(query: str, limit: int, *, generation: str) -> str:
    return f"{ENTRY_PREFIX}{generation}:{_query_digest(query, limit)}"


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite cached number")
    return number


def _reject_constant(value: str):
    raise ValueError("Invalid cached JSON constant")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate cached JSON field")
        result[key] = value
    return result


def _valid_shape(results, limit):
    if not isinstance(results, list) or len(results) > limit or not all(isinstance(item, dict) for item in results):
        return False
    pending = [(results, 1)]
    while pending:
        value, depth = pending.pop()
        if not isinstance(value, (list, dict)):
            continue
        if depth > MAX_CACHE_NESTING:
            return False
        children = value.values() if isinstance(value, dict) else value
        pending.extend((child, depth + 1) for child in children if isinstance(child, (list, dict)))
    return True


def get_cached_search(query: str, limit: int) -> CacheLookup:
    try:
        reply = redis_client.eval(
            _READ_SCRIPT, 1, GENERATION_KEY, uuid4().hex,
            ENTRY_PREFIX, _query_digest(query, limit),
        )
    except Exception:
        # Unknown generation means retrieval can continue, but cannot cache.
        return CacheLookup()

    if not isinstance(reply, (list, tuple)) or len(reply) != 2:
        return CacheLookup()
    generation, value = reply
    if not isinstance(generation, str) or not generation:
        return CacheLookup()

    if value is None:
        return CacheLookup(generation=generation)
    try:
        results = json.loads(
            value, parse_float=_finite_float, parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
        if not _valid_shape(results, limit):
            raise ValueError("Invalid cached result shape")
    except (TypeError, ValueError, RecursionError):
        return CacheLookup(generation=generation)
    return CacheLookup(results=results, generation=generation)


def set_cached_search(
    query: str, limit: int, results: list[dict], *, generation: str | None,
) -> bool:
    # Never obtain a new token after retrieval: that would admit stale results.
    if not isinstance(generation, str) or not generation:
        return False
    if not _valid_shape(results, limit):
        return False
    try:
        return bool(redis_client.eval(
            _WRITE_SCRIPT, 2, GENERATION_KEY,
            build_cache_key(query, limit, generation=generation),
            generation, settings.search_cache_ttl, json.dumps(results, allow_nan=False),
        ))
    except Exception:
        # Redis failure must not prevent returning retrieved results.
        return False


def invalidate_search_cache(*, strict: bool = False) -> int:
    """Rotate generation atomically; return the number of old keys cleaned up.

    Correctness relies on rotation, not physical deletion. Strict mode surfaces
    rotation failures so sync remains retryable. Cleanup is best-effort; old
    entries retain their original TTL and cannot be served by a newer generation.
    """
    try:
        previous = redis_client.getset(GENERATION_KEY, uuid4().hex)
    except Exception:
        if strict:
            raise
        return 0

    if previous is None:
        return 0
    deleted = 0
    batch = []
    try:
        for key in redis_client.scan_iter(match=f"{ENTRY_PREFIX}{previous}:*", count=256):
            batch.append(key)
            if len(batch) == 256:
                deleted += redis_client.delete(*batch)
                batch.clear()
        if batch:
            deleted += redis_client.delete(*batch)
    except Exception:
        # Logical invalidation already succeeded; cleanup may wait for expiry.
        pass
    return deleted
