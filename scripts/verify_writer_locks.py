"""Real PostgreSQL coordination probe; no schema or metadata writes."""
import json
import secrets
from contextlib import ExitStack

from sqlalchemy import select, text

from app.db.database import SessionLocal
from app.db.models import (IndexPublicationJob, Repository, RepositoryFullIndexJob,
                           RepositorySyncJob)
from app.indexer.locking import (RepositorySyncInProgress, generation_maintenance_lock,
                                 repository_sync_lock)


def _preflight(db, repository_ids):
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Live coordination verification requires PostgreSQL.")
    for model in (IndexPublicationJob, RepositorySyncJob, RepositoryFullIndexJob):
        if db.execute(select(model).limit(1)).first() is not None:
            raise RuntimeError("Pending writer work blocks verification.")
    if db.execute(select(Repository.id).where(Repository.id.in_(repository_ids))).first():
        raise RuntimeError("Probe repository ID is registered.")
    db.rollback()


def _blocked(context):
    try:
        with context:
            raise AssertionError("Conflicting lock was acquired.")
    except RepositorySyncInProgress:
        return


def _commit_read(db):
    if db.execute(text("SELECT 1")).scalar_one() != 1:
        raise RuntimeError("Metadata read failed.")
    db.commit()


def verify_writer_locks(session_factory=SessionLocal):
    ids = (1_000_000_000 + secrets.randbelow(500_000_000),
           1_500_000_000 + secrets.randbelow(500_000_000))
    checks = []
    with ExitStack() as stack:
        owner, contender, other = [stack.enter_context(session_factory()) for _ in range(3)]
        _preflight(owner, ids)
        # Fail before probing if the corpus is currently held by any writer.
        with generation_maintenance_lock(owner):
            _preflight(owner, ids)
        with repository_sync_lock(owner, ids[0]):
            with repository_sync_lock(other, ids[1]):
                _commit_read(other)
            _commit_read(owner)
            _blocked(repository_sync_lock(contender, ids[0]))
            _blocked(repository_sync_lock(contender, ids[1], publication=True))
            _blocked(generation_maintenance_lock(contender))
        # Detect leaked shared corpus locks from failed partial acquisitions.
        with generation_maintenance_lock(contender):
            pass
        checks.append("shared_overlap_same_repository_exclusion_commit_and_partial_release")

        for label, context in (
            ("publisher", repository_sync_lock(owner, ids[0], publication=True)),
            ("maintenance", generation_maintenance_lock(owner)),
        ):
            with context:
                _commit_read(owner)
                _blocked(repository_sync_lock(contender, ids[1]))
                _blocked(repository_sync_lock(contender, ids[1], publication=True))
                _blocked(generation_maintenance_lock(contender))
            with repository_sync_lock(contender, ids[1]):
                pass
            checks.append(label + "_exclusion_across_commit_and_release")

        class InjectedFailure(Exception):
            pass

        for context in (repository_sync_lock(owner, ids[0]),
                        repository_sync_lock(owner, ids[0], publication=True),
                        generation_maintenance_lock(owner)):
            try:
                with context:
                    _commit_read(owner)
                    raise InjectedFailure()
            except InjectedFailure:
                pass
            with generation_maintenance_lock(contender):
                pass
            with repository_sync_lock(contender, ids[0]):
                pass
        checks.append("writer_publisher_maintenance_exception_release")
    return {"status": "passed", "checks": checks, "metadata_writes": False,
            "schema_changes": False, "probe_sessions_closed": True}


def main():
    try:
        report = verify_writer_locks()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Coordination verification failed; no success claimed."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
