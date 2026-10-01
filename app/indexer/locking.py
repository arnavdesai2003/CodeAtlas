"""Shared coordination for repository metadata and search-index writers."""
from contextlib import contextmanager
from sqlalchemy import text
from sqlalchemy.orm import Session


class RepositorySyncInProgress(RuntimeError):
    pass


@contextmanager
def repository_sync_lock(db: Session, repository_id: int):
    """Session advisory lock survives the metadata transaction's commit.

    SQLite is used only by single-threaded offline transaction tests. Production
    PostgreSQL sync and full-index callers share this lock across processes.
    """
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        yield
        return
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Repository writer coordination requires PostgreSQL.")
    params = {"namespace": 0x4341544C, "repository_id": repository_id}
    with bind.connect() as connection:
        try:
            acquired = connection.execute(text(
                "SELECT pg_try_advisory_lock(:namespace, :repository_id)"
            ), params).scalar_one()
        except Exception:
            connection.invalidate()
            raise
        if not acquired:
            raise RepositorySyncInProgress("A repository writer is already in progress.")
        try:
            yield
        finally:
            try:
                connection.execute(text(
                    "SELECT pg_advisory_unlock(:namespace, :repository_id)"
                ), params)
            except Exception:
                # Never return a connection with a session lock to the pool.
                connection.invalidate()
                raise
