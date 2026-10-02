from time import perf_counter
from concurrent.futures import TimeoutError as FutureTimeoutError
from copy import deepcopy

from app.search.cache import (
    CacheLookup,
    get_cached_search,
    set_cached_search,
)
from app.search.engine import search_code
from app.search.coalescing import MissFlights


_miss_flights = MissFlights()


def _hit_response(results, total_start):
    return {
        "results": results, "cache_hit": True, "cache_coalesced": False,
        "coalescing_wait_ms": 0.0,
        "search_latency_ms": round((perf_counter() - total_start) * 1000, 3),
    }


def _retrieve_response(query, limit, lookup, total_start, *, bypass_cache, wait_ms=0.0):
    start = perf_counter()
    results = search_code(query=query, limit=limit)
    engine_ms = (perf_counter() - start) * 1000
    if not bypass_cache and lookup.generation is not None:
        set_cached_search(query=query, limit=limit, results=results, generation=lookup.generation)
    return {
        "results": results, "cache_hit": False, "cache_coalesced": False,
        "coalescing_wait_ms": round(wait_ms, 3),
        "elasticsearch_latency_ms": round(engine_ms, 3),
        "search_latency_ms": round((perf_counter() - total_start) * 1000, 3),
    }


def search_with_cache(
    query: str,
    limit: int,
    *,
    bypass_cache: bool = False,
) -> dict:
    total_start = perf_counter()

    lookup = CacheLookup() if bypass_cache else get_cached_search(
        query=query,
        limit=limit,
    )
    if lookup.results is not None:
        return _hit_response(lookup.results, total_start)
    if bypass_cache or lookup.generation is None:
        return _retrieve_response(query, limit, lookup, total_start, bypass_cache=bypass_cache)

    # Exact input text preserves retrieval semantics. Generation prevents a new
    # reader after invalidation from joining an older in-flight computation.
    key = (query, limit, lookup.generation)
    future, leader = _miss_flights.acquire(key)
    if future is None:
        return _retrieve_response(query, limit, lookup, total_start, bypass_cache=False)
    if not leader:
        wait_start = perf_counter()
        try:
            shared = future.result(timeout=_miss_flights.wait_seconds)
        except FutureTimeoutError:
            # A leader's own TimeoutError must propagate rather than be
            # mistaken for an expired follower wait. A late success is usable.
            if future.done():
                shared = future.result()
            else:
                return _retrieve_response(query, limit, lookup, total_start, bypass_cache=False,
                    wait_ms=(perf_counter() - wait_start) * 1000)
        wait_ms = (perf_counter() - wait_start) * 1000
        response = deepcopy(shared)
        response.update(cache_hit=False, cache_coalesced=True,
                        coalescing_wait_ms=round(wait_ms, 3),
                        search_latency_ms=round((perf_counter() - total_start) * 1000, 3))
        # This caller waited; it did not execute an Elasticsearch engine call.
        response.pop("elasticsearch_latency_ms", None)
        return response

    try:
        # Another leader may have filled Redis after our first miss but before
        # this slot was acquired. Do not adopt a newer generation for old work.
        again = get_cached_search(query=query, limit=limit)
        if again.generation == lookup.generation and again.results is not None:
            response = _hit_response(again.results, total_start)
        else:
            response = _retrieve_response(query, limit, lookup, total_start, bypass_cache=False)
        future.set_result(deepcopy(response))
    except BaseException as error:
        future.set_exception(error)
        raise
    finally:
        _miss_flights.release(key, future)
    # Include result publication/copying and registry cleanup in the leader's
    # service timing. Followers replace the shared timing with their own.
    response["search_latency_ms"] = round((perf_counter() - total_start) * 1000, 3)
    return response
