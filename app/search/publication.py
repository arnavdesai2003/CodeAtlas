"""Recoverable whole-index staging with one atomic alias switch.

The caller holds the exclusive corpus advisory lock. A singleton database job
keeps all other cooperating writers blocked across crashes and retries.
"""
from collections.abc import Mapping
from uuid import uuid4

from app.db.models import (
    CodeSymbol, Repository, RepositoryFullIndexJob, RepositorySyncJob,
    IndexPublicationJob,
)
from app.search.cache import invalidate_search_cache
from app.search.indexes import alias_target
from app.search.generation_lifecycle import (
    begin_build_attempt, remember_active_source, remember_created_stage,
    remember_publication, verify_recorded_index,
)


def _check_response(response, operation: str) -> None:
    failures = response.get("failures") if isinstance(response, Mapping) else None
    conflicts = response.get("version_conflicts") if isinstance(response, Mapping) else None
    if (
        not isinstance(response, Mapping)
        or response.get("timed_out") is not False
        or not isinstance(failures, list) or failures
        or type(conflicts) is not int or conflicts != 0
    ):
        raise RuntimeError(f"Elasticsearch {operation} was incomplete; retry full indexing.")


def checked_count(client, **kwargs) -> int:
    response = client.count(**kwargs)
    if not isinstance(response, Mapping):
        raise RuntimeError("Elasticsearch publication count was incomplete; retry full indexing.")
    shards = response.get("_shards")
    failed = shards.get("failed") if isinstance(shards, Mapping) else None
    count = response.get("count")
    if (
        ("timed_out" in response and response["timed_out"] is not False)
        or type(failed) is not int or failed != 0
    ):
        raise RuntimeError("Elasticsearch publication count was incomplete; retry full indexing.")
    if type(count) is not int or count < 0:
        raise RuntimeError("Elasticsearch publication count was invalid; retry full indexing.")
    return count


def create_staging_index(client, *, source: str, stage: str) -> None:
    # Preserve effective vector options/analyzers rather than letting a future
    # Elasticsearch version choose different defaults for a new generation.
    mappings = client.indices.get_mapping(index=source)[source]["mappings"]
    source_settings = client.indices.get_settings(index=source)[source]["settings"]["index"]
    settings = {key: source_settings[key] for key in (
        "number_of_shards", "number_of_replicas", "refresh_interval",
        "analysis", "similarity", "mapping",
    ) if key in source_settings}
    response = client.indices.create(index=stage, mappings=mappings, settings=settings)
    if (
        not isinstance(response, Mapping)
        or response.get("acknowledged") is not True
        or response.get("shards_acknowledged") is not True
    ):
        raise RuntimeError("Staging index creation was not acknowledged; retry full indexing.")


def publish_repository_index(db, repository_id: int) -> dict:
    from app.search import engine

    if db.get(RepositorySyncJob, repository_id) is not None:
        raise RuntimeError("Finish pending repository synchronization before full Elasticsearch indexing.")
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise ValueError(f"Repository {repository_id} does not exist.")
    full_job = db.get(RepositoryFullIndexJob, repository_id)
    job = db.get(IndexPublicationJob, 1)
    resumed = job is not None or full_job is not None
    if job is not None and job.repository_id != repository_id:
        raise RuntimeError(f"Resume full indexing for repository {job.repository_id} first.")
    if job is None:
        source = engine.resolve_search_index()
        # Only initial provisioning may create the legacy index. Never recreate
        # a missing published generation: that would hide loss of committed data.
        if source == engine.INDEX_NAME:
            engine.create_symbol_index(index_name=source)
        stage = f"{engine.INDEX_NAME}_generation_{uuid4().hex}"
        job = IndexPublicationJob(
            id=1, repository_id=repository_id, source_index=source,
            staging_index=stage, phase="building", stats={},
        )
        db.add(job)
        if full_job is None:
            full_job = RepositoryFullIndexJob(repository_id=repository_id, stats={})
            db.add(full_job)
        db.commit()

    client = engine.elasticsearch_client.options(request_timeout=60)
    active = engine.resolve_search_index()
    if active == job.staging_index:
        # The alias switch may have succeeded despite a lost acknowledgement or
        # failed metadata commit. Never reset/reindex an already published stage.
        if job.phase not in {"ready", "published"}:
            raise RuntimeError("Published staging index has no validated preparation record.")
    else:
        if active != job.source_index:
            raise RuntimeError("Active index changed outside the publication protocol; reconcile manually.")
        if job.phase == "published":
            raise RuntimeError("Published alias was changed outside the publication protocol.")
        if job.phase == "building":
            # A timed-out ES request may still be writing. Each build attempt
            # gets a fresh journaled name; never reuse/delete an ambiguous stage.
            remember_active_source(db, client, source=job.source_index, legacy_name=engine.INDEX_NAME)
            begin_build_attempt(db, job,
                stage=f"{engine.INDEX_NAME}_generation_{uuid4().hex}")
            db.commit()
            create_staging_index(client, source=job.source_index, stage=job.staging_index)
            remember_created_stage(db, client, job.staging_index)
            db.commit()
            query = {"bool": {"must_not": [{"term": {"repository_id": repository_id}}]}}
            expected_copied = checked_count(client, index=job.source_index, query=query)
            response = client.reindex(
                source={"index": job.source_index, "query": query},
                dest={"index": job.staging_index}, refresh=True,
                wait_for_completion=True,
            )
            _check_response(response, "staging copy")
            total = response.get("total")
            created = response.get("created")
            if (
                type(total) is not int or total < 0 or total != expected_copied
                or type(created) is not int or created < 0 or created != expected_copied
            ):
                raise RuntimeError("Elasticsearch staging copy count did not match the source.")
            result = engine._write_repository_index(db, repository_id, index_name=job.staging_index)
            expected_symbols = db.query(CodeSymbol).filter_by(repository_id=repository_id).count()
            if result["symbols_indexed"] != expected_symbols:
                raise RuntimeError("Staged symbol count did not match PostgreSQL.")
            engine.refresh_symbol_index(job.staging_index, client=client)
            expected_total = expected_copied + expected_symbols
            if checked_count(client, index=job.staging_index) != expected_total:
                raise RuntimeError("Staging index count did not match the prepared snapshot.")
            job.stats = {**result, "documents_copied": expected_copied, "documents_total": expected_total}
            job.phase = "ready"
            db.commit()
        if job.phase != "ready":
            raise RuntimeError("Unknown index publication phase.")
        verify_recorded_index(db, client, job.source_index)
        verify_recorded_index(db, client, job.staging_index)
        # Validate a resumed ready stage before publication as well.
        if checked_count(client, index=job.staging_index) != job.stats["documents_total"]:
            raise RuntimeError("Prepared staging index changed; reconcile before publication.")
        previous_alias = alias_target(client, alias_name=engine.SEARCH_ALIAS)
        if (previous_alias or engine.INDEX_NAME) != job.source_index:
            raise RuntimeError("Active alias changed before publication; reconcile manually.")
        actions = []
        if previous_alias is not None:
            actions.append({"remove": {"index": previous_alias, "alias": engine.SEARCH_ALIAS,
                                       "must_exist": True}})
        actions.append({"add": {"index": job.staging_index, "alias": engine.SEARCH_ALIAS,
                                "is_write_index": True}})
        response = client.indices.update_aliases(actions=actions)
        if (
            not isinstance(response, Mapping)
            or response.get("acknowledged") is not True
            or ("errors" in response and response["errors"] is not False)
        ):
            raise RuntimeError("Alias publication was not acknowledged; retry to inspect its outcome.")
        if engine.resolve_search_index() != job.staging_index:
            raise RuntimeError("Published alias does not reference the prepared staging index.")

    remember_publication(db, client, job, legacy_name=engine.INDEX_NAME)
    job.phase = "published"
    db.commit()
    # A Redis outage keeps the published job and writer exclusion in place.
    # Retry rotates again, fencing fills from before either invalidation.
    invalidated = invalidate_search_cache(strict=True)
    result = {**job.stats, "resumed": resumed, "cache_entries_invalidated": invalidated,
              "source_index": job.source_index, "index": job.staging_index,
              "publication": "atomic_alias"}
    if full_job is not None:
        db.delete(full_job)
    db.delete(job)
    db.commit()
    # Retain the source generation for in-flight requests and operator rollback.
    return result
