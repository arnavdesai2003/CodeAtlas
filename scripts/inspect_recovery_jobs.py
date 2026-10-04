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
    resumable_owners = {job.repository_id for job in publication
        if job.phase in {"building", "ready", "published"} and job.repository_id not in sync_ids}
    conflicts = sorted(full_ids & sync_ids)
    return {
        "status": "observed", "read_only": True,
        "snapshot": "repeatable_read" if dialect == "postgresql" else "offline_sqlite",
        "publication_blocks_other_writers": bool(publication),
        "conflicting_sync_and_full_repository_ids": conflicts,
        "sync_jobs": [{"repository_id": job.repository_id,
            "old_commit": job.old_commit, "target_commit": job.target_commit,
            "affected_path_count": len(job.affected_paths), "file_count": len(job.file_ids),
            "resume": None if publication or job.repository_id in conflicts else
                f"POST /repositories/{job.repository_id}/sync"} for job in sync],
        "full_index_jobs": [{"repository_id": job.repository_id,
            "resume": None if job.repository_id in conflicts or (owners and job.repository_id not in resumable_owners) else
                f".venv/bin/python -m scripts.index_elasticsearch --repository-id {job.repository_id}"}
            for job in full],
        "publication_jobs": [{"repository_id": job.repository_id, "phase": job.phase,
            "source_index": job.source_index, "staging_index": job.staging_index,
            "resume": None if job.phase not in {"building", "ready", "published"} or job.repository_id in sync_ids else
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
