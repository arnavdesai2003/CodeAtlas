"""Lifecycle timestamps and identities for conservative generation retention."""
from datetime import datetime, timezone
import re

from elasticsearch import NotFoundError
from app.db.models import SearchIndexGeneration


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_generation_name(name: str, legacy_name: str) -> bool:
    return re.fullmatch(re.escape(legacy_name) + r"_generation_[0-9a-f]{32}", name) is not None


def index_uuid(client, name: str) -> str:
    value = client.indices.get_settings(index=name)[name]["settings"]["index"].get("uuid")
    if not isinstance(value, str) or not value or value == "_na_":
        raise RuntimeError("Elasticsearch did not provide a usable index identity.")
    return value


def _verify_identity(row, identity: str) -> None:
    if row.index_uuid is not None and row.index_uuid != identity:
        raise RuntimeError("Generation name now references another index identity; reconcile manually.")
    row.index_uuid = identity


def remember_active_source(db, client, *, source: str, legacy_name: str):
    if not is_generation_name(source, legacy_name):
        return
    identity = index_uuid(client, source)
    row = db.get(SearchIndexGeneration, source)
    if row is None:
        row = SearchIndexGeneration(index_name=source, state="published", created_at=utc_now())
        db.add(row)
    _verify_identity(row, identity)
    row.state = "published"
    row.published_at = row.published_at or utc_now()
    row.inactive_at = None
    row.deleted_at = None


def begin_build_attempt(db, job, *, stage: str):
    previous = db.get(SearchIndexGeneration, job.staging_index)
    if previous is not None:
        if previous.state != "building":
            raise RuntimeError("Unpublished staging record has an unexpected lifecycle state.")
        previous.state = "abandoned"
        previous.inactive_at = utc_now()
    # Untracked old-version attempts stay untracked: their history/late writes
    # cannot be inferred from creation age. The inspector protects them.
    job.staging_index = stage
    job.stats = {}
    db.add(SearchIndexGeneration(index_name=stage, repository_id=job.repository_id,
        state="building", created_at=utc_now()))


def remember_created_stage(db, client, name: str):
    row = db.get(SearchIndexGeneration, name)
    if row is None:
        raise RuntimeError("Staging identity has no durable lifecycle record.")
    _verify_identity(row, index_uuid(client, name))


def verify_recorded_index(db, client, name: str):
    row = db.get(SearchIndexGeneration, name)
    if row is not None:
        _verify_identity(row, index_uuid(client, name))


def remember_publication(db, client, job, *, legacy_name: str):
    # Also adopts a ready/published job created by the preceding version.
    stage = db.get(SearchIndexGeneration, job.staging_index)
    if stage is None:
        stage = SearchIndexGeneration(index_name=job.staging_index,
            repository_id=job.repository_id, state="building", created_at=utc_now())
        db.add(stage)
    _verify_identity(stage, index_uuid(client, job.staging_index))
    stage.state = "published"
    stage.published_at = stage.published_at or utc_now()
    stage.inactive_at = None
    stage.deleted_at = None
    if is_generation_name(job.source_index, legacy_name):
        source = db.get(SearchIndexGeneration, job.source_index)
        if source is None:
            # A previous-version source is adopted only after observing it as
            # the recorded source of a confirmed publication.
            try:
                identity = index_uuid(client, job.source_index)
            except NotFoundError:
                return  # Do not invent cleanup history for a missing old source.
            source = SearchIndexGeneration(index_name=job.source_index, index_uuid=identity,
                state="published", created_at=utc_now())
            db.add(source)
        else:
            _verify_identity(source, index_uuid(client, job.source_index))
        if source.state != "retired":
            source.state = "retired"
            # Confirmation time, never creation time; a recently retired old
            # index must receive the entire grace period.
            source.inactive_at = utc_now()
