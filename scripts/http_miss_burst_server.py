"""Factory for the loopback HTTP diagnostic; never use for deployment."""

from contextlib import asynccontextmanager
import json
import os
import re

from app.core.config import settings
from app.search import cache, service
from app.search.embeddings import get_embedding_model
from scripts.benchmark_api import QUERIES


def validate_namespace(prefix):
    if not re.fullmatch(r"codeatlas:diagnostic:httpburst:[0-9a-f]{32}:", prefix):
        raise ValueError("A unique diagnostic-only Redis namespace is required.")
    return prefix


def create_app():
    if settings.app_env not in {"development", "test"}:
        raise RuntimeError("HTTP diagnostic factory requires development/test mode.")
    prefix = validate_namespace(os.environ.get("CODEATLAS_HTTP_BURST_NAMESPACE", ""))
    cache.CACHE_PREFIX = prefix
    cache.ENTRY_PREFIX = prefix + "entries:"
    cache.GENERATION_KEY = prefix + "generation"
    original_search = service.search_code
    pid = str(os.getpid())

    def counted_search(*args, **kwargs):
        # Diagnostic overhead, applied to before/after alike. Do not interpret
        # these timings as uninstrumented production capacity.
        cache.redis_client.hincrby(prefix + "engine_calls", pid, 1)
        return original_search(*args, **kwargs)

    service.search_code = counted_search
    from app.main import app
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            for query in QUERIES:
                original_search(query=query, limit=10)
            cache.redis_client.hset(prefix + "ready", pid, json.dumps({
                "pid": pid, "service_file": service.__file__,
                "coalescing": hasattr(service, "_miss_flights"),
                "device": str(get_embedding_model().device),
                "torch_num_threads": settings.torch_num_threads,
                "close_search_connections": settings.elasticsearch_close_search_connections,
            }))
            yield

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def worker_header(request, call_next):
        response = await call_next(request)
        response.headers["X-CodeAtlas-Diagnostic-Worker"] = pid
        return response

    return app
