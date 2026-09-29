import hashlib
import json

from app.core.clients import redis_client
from app.core.config import settings


CACHE_PREFIX = "codeatlas:search:"


def build_cache_key(
    query: str,
    limit: int,
) -> str:
    normalized_query = " ".join(
        query.lower().strip().split()
    )

    raw_key = f"{normalized_query}:{limit}"

    digest = hashlib.sha256(
        raw_key.encode("utf-8")
    ).hexdigest()

    return f"{CACHE_PREFIX}{digest}"


def get_cached_search(
    query: str,
    limit: int,
) -> list[dict] | None:
    key = build_cache_key(
        query=query,
        limit=limit,
    )

    try:
        cached_value = redis_client.get(key)

        if cached_value is None:
            return None

        return json.loads(cached_value)

    except Exception:
        # Redis failure should not break search.
        return None


def set_cached_search(
    query: str,
    limit: int,
    results: list[dict],
) -> None:
    key = build_cache_key(
        query=query,
        limit=limit,
    )

    try:
        redis_client.setex(
            key,
            settings.search_cache_ttl,
            json.dumps(results),
        )

    except Exception:
        # Elasticsearch results should still be returned
        # even if Redis is unavailable.
        pass

def invalidate_search_cache(*, strict: bool = False) -> int:
    try:
        keys = list(
            redis_client.scan_iter(
                match=f"{CACHE_PREFIX}*"
            )
        )

        if not keys:
            return 0

        return redis_client.delete(*keys)

    except Exception:
        if strict:
            raise
        return 0
