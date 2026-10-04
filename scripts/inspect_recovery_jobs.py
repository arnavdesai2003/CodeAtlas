"""Read pending metadata journals without contacting Elasticsearch or Redis."""
import argparse
import json
from sqlalchemy import select, text

from app.db.database import SessionLocal
from app.db.models import IndexPublicationJob, RepositoryFullIndexJob, RepositorySyncJob
from app.indexer.locking import validate_repository_id


def inspect_recovery_jobs(db):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    elif dialect != "sqlite":
        raise RuntimeError("Recovery inspection requires PostgreSQL.")
    sync = db.scalars(select(RepositorySyncJob).order_by(RepositorySyncJob.repository_id)).all()
    full = db.scalars(select(RepositoryFullIndexJob).order_by(RepositoryFullIndexJob.repository_id)).all()
    publication = db.scalars(select(IndexPublicationJob).order_by(IndexPublicationJob.id)).all()
    for job in [*sync, *full, *publication]:
        validate_repository_id(job.repository_id)
    for job in sync:
        if not isinstance(job.affected_paths, list) or any(
            not isinstance(path, str) or not path for path in job.affected_paths
        ):
            raise RuntimeError("Sync journal path metadata is malformed.")
        if not isinstance(job.file_ids, list):
            raise RuntimeError("Sync journal file metadata is malformed.")
        for file_id in job.file_ids:
            validate_repository_id(file_id)
    full_ids = {job.repository_id for job in full}
    sync_ids = {job.repository_id for job in sync}
    owners = {job.repository_id for job in publication}
    conflicts = sorted(full_ids & sync_ids)
    publication_reasons = {job.repository_id: (
        "publication_owner_has_pending_sync" if job.repository_id in sync_ids else
        "unknown_publication_phase" if job.phase not in {"building", "ready", "published"} else None
    ) for job in publication}
    sync_reasons = {job.repository_id: (
        "conflicting_sync_and_full_jobs" if job.repository_id in conflicts else
        "pending_publication_blocks_sync" if publication else None
    ) for job in sync}
    full_reasons = {job.repository_id: (
        "conflicting_sync_and_full_jobs" if job.repository_id in conflicts else
        "another_repository_owns_publication" if owners and job.repository_id not in owners else
        publication_reasons.get(job.repository_id)
    ) for job in full}
    return {
        "status": "observed", "read_only": True,
        "snapshot": "repeatable_read" if dialect == "postgresql" else "offline_sqlite",
        "publication_blocks_other_writers": bool(publication),
        "conflicting_sync_and_full_repository_ids": conflicts,
        "conflicting_publication_and_sync_repository_ids": sorted(owners & sync_ids),
        "sync_jobs": [{"repository_id": job.repository_id,
            "old_commit": job.old_commit, "target_commit": job.target_commit,
            "affected_path_count": len(job.affected_paths), "file_count": len(job.file_ids),
            "blocked_reason": sync_reasons[job.repository_id],
            "resume": None if sync_reasons[job.repository_id] else
                f"POST /repositories/{job.repository_id}/sync"} for job in sync],
        "full_index_jobs": [{"repository_id": job.repository_id,
            "blocked_reason": full_reasons[job.repository_id],
            "resume": None if full_reasons[job.repository_id] else
                f".venv/bin/python -m scripts.index_elasticsearch --repository-id {job.repository_id}"}
            for job in full],
        "publication_jobs": [{"repository_id": job.repository_id, "phase": job.phase,
            "source_index": job.source_index, "staging_index": job.staging_index,
            "blocked_reason": publication_reasons[job.repository_id],
            "resume": None if publication_reasons[job.repository_id] else
                f".venv/bin/python -m scripts.index_elasticsearch --repository-id {job.repository_id}"}
            for job in publication],
    }


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        with SessionLocal() as db:
            report = inspect_recovery_jobs(db)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Recovery metadata inspection failed; no state was changed."}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
