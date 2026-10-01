"""Shared coordination for repository metadata and search-index writers."""
from contextlib import contextmanager
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db.models import IndexPublicationJob


class RepositorySyncInProgress(RuntimeError):
    pass


@contextmanager
def _writer_locks(db: Session, locks):
    """Hold PostgreSQL session locks on one connection, release in reverse order."""
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        yield
        return
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Repository writer coordination requires PostgreSQL.")
    acquired = []
    invalidated = False
    with bind.connect() as connection:
        try:
            for key, suffix in locks:
                params = {"namespace": 0x4341544C, "repository_id": key}
                try:
                    owns_lock = connection.execute(text(
                        f"SELECT pg_try_advisory_lock{suffix}(:namespace, :repository_id)"
                    ), params).scalar_one()
                except Exception:
                    connection.invalidate()
                    invalidated = True
                    raise
                if not owns_lock:
                    raise RepositorySyncInProgress("A repository writer is already in progress.")
                acquired.append((params, suffix))
            yield
        finally:
            for params, suffix in reversed(acquired) if not invalidated else ():
                try:
                    released = connection.execute(text(
                        f"SELECT pg_advisory_unlock{suffix}(:namespace, :repository_id)"
                    ), params).scalar_one()
                    if not released:
                        raise RuntimeError("Repository writer lock was lost.")
                except Exception:
                    connection.invalidate()
                    raise


@contextmanager
def repository_sync_lock(db: Session, repository_id: int, *, publication: bool = False):
    """Shared corpus lock for normal writers; exclusive for staged publication.

    PostgreSQL session locks survive metadata commits. SQLite is supported only
    for single-threaded offline tests. A pending journal blocks writers even
    after a publisher crashes and its advisory locks disappear.
    """
    suffix = "" if publication else "_shared"
    with _writer_locks(db, ((-1, suffix), (repository_id, ""))):
        pending = db.get(IndexPublicationJob, 1, populate_existing=True)
        if pending is not None and not (publication and pending.repository_id == repository_id):
            raise RepositorySyncInProgress(
                f"Resume full Elasticsearch indexing for repository {pending.repository_id} "
                "before running another repository writer."
            )
        yield


@contextmanager
def generation_maintenance_lock(db: Session):
    """Exclude cooperating writers while rechecking and applying retention."""
    with _writer_locks(db, ((-1, ""),)):
        yield
