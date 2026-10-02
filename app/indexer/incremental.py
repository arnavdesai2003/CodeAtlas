from pathlib import Path
import subprocess

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import (
    CodeFile,
    CodeSymbol,
    Repository,
    RepositorySyncJob,
    RepositoryFullIndexJob,
)
from app.indexer.locking import RepositorySyncInProgress, repository_sync_lock
from app.indexer.parser import parse_python_source
from app.indexer.errors import RepositoryNotFound, RepositoryCloneMissing
from app.indexer.paths import regular_source_path, clone_directory
from app.indexer.git import git_output
from app.indexer.repository import (
    LANGUAGE_BY_EXTENSION,
    REPOSITORY_ROOT,
    calculate_file_hash,
    parse_github_url,
)
from app.search.cache import invalidate_search_cache
from app.search.engine import (
    delete_paths_from_elasticsearch,
    index_files_in_elasticsearch,
)


def run_git(
    repository_path: Path,
    *args: str,
) -> str:
    return git_output("-C", str(repository_path), *args)


def parse_git_diff(output: str) -> list[dict]:
    changes = []

    for line in output.splitlines():
        if not line.strip():
            continue

        parts = line.split("\t")
        status = parts[0]

        # Rename:
        # R100    old/path.py    new/path.py
        if status.startswith("R"):
            changes.append(
                {
                    "status": "R",
                    "old_path": parts[1],
                    "path": parts[2],
                }
            )
            continue

        if len(parts) < 2:
            continue

        changes.append(
            {
                "status": status[0],
                "path": parts[1],
            }
        )

    return changes


def sync_repository(db: Session, repository_id: int) -> dict:
    try:
        with repository_sync_lock(db, repository_id):
            return _sync_repository(db, repository_id)
    except Exception:
        db.rollback()
        raise


def _finish_sync(db: Session, repository: Repository, job: RepositorySyncJob, *, resumed: bool) -> dict:
    # Safe to replay after any partial delete, bulk-index or final commit failure.
    delete_paths_from_elasticsearch(repository_id=repository.id, paths=job.affected_paths)
    symbols_indexed = index_files_in_elasticsearch(db=db, file_ids=job.file_ids)
    invalidated = invalidate_search_cache(strict=True)
    result = {
        "repository_id": repository.id,
        "repository": repository.name,
        "changed": True,
        "resumed": resumed,
        "old_commit": job.old_commit,
        "new_commit": job.target_commit,
        **job.stats,
        "symbols_indexed": symbols_indexed,
        "cache_entries_invalidated": invalidated,
    }
    repository.last_indexed_commit = job.target_commit
    db.delete(job)
    db.commit()
    return result


def _sync_repository(
    db: Session,
    repository_id: int,
) -> dict:
    repository = db.get(
        Repository,
        repository_id,
        populate_existing=True,
    )

    if repository is None:
        raise RepositoryNotFound(
            f"Repository {repository_id} does not exist."
        )

    if db.get(RepositoryFullIndexJob, repository_id) is not None:
        raise RepositorySyncInProgress("Finish pending full Elasticsearch indexing before synchronization.")

    pending = db.get(RepositorySyncJob, repository_id)
    if pending is not None:
        # Finish the recorded target before fetching a newer remote commit.
        # Metadata/symbol IDs from that target are already durably committed.
        return _finish_sync(db, repository, pending, resumed=True)

    owner, repository_name = parse_github_url(
        repository.clone_url
    )

    repository_path = clone_directory(REPOSITORY_ROOT, owner, repository_name)

    if not repository_path.exists():
        raise RepositoryCloneMissing(
            f"Repository directory not found: "
            f"{repository_path}"
        )

    old_commit = repository.last_indexed_commit

    if old_commit is None:
        old_commit = run_git(
            repository_path,
            "rev-parse",
            "HEAD",
        )

    branch = repository.default_branch

    # Fetch newest commit without recloning.
    run_git(
        repository_path,
        "fetch",
        "--prune",
        "origin",
    )

    new_commit = run_git(
        repository_path,
        "rev-parse",
        f"origin/{branch}",
    )

    if old_commit == new_commit:
        return {
            "repository_id": repository.id,
            "repository": repository.name,
            "changed": False,
            "old_commit": old_commit,
            "new_commit": new_commit,
            "files_added": 0,
            "files_modified": 0,
            "files_deleted": 0,
            "files_renamed": 0,
            "files_parsed": 0,
            "symbols_indexed": 0,
            "cache_entries_invalidated": 0,
        }

    diff_output = run_git(
        repository_path,
        "diff",
        "--name-status",
        old_commit,
        new_commit,
    )

    changes = parse_git_diff(
        diff_output
    )

    # Move working tree to newest fetched commit.
    run_git(
        repository_path,
        "reset",
        "--hard",
        new_commit,
    )

    affected_paths: set[str] = set()
    updated_file_ids: list[int] = []

    files_added = 0
    files_modified = 0
    files_deleted = 0
    files_renamed = 0
    files_parsed = 0

    try:
        for change in changes:
            status = change["status"]

            # Handle deletion or rename of old path.
            if status in {"D", "R"}:
                old_path = (
                    change["old_path"]
                    if status == "R"
                    else change["path"]
                )

                affected_paths.add(old_path)

                existing_file = (
                    db.query(CodeFile)
                    .filter(
                        CodeFile.repository_id
                        == repository.id,
                        CodeFile.path == old_path,
                    )
                    .first()
                )

                if existing_file is not None:
                    db.execute(
                        delete(CodeSymbol).where(
                            CodeSymbol.file_id
                            == existing_file.id
                        )
                    )

                    db.delete(existing_file)

                if status == "D":
                    files_deleted += 1
                    continue

                files_renamed += 1

            path = change["path"]

            affected_paths.add(path)

            extension = (
                Path(path).suffix.lower()
            )

            language = LANGUAGE_BY_EXTENSION.get(
                extension
            )

            # Ignore unsupported file types.
            if language is None:
                continue

            code_file = (
                db.query(CodeFile)
                .filter(
                    CodeFile.repository_id
                    == repository.id,
                    CodeFile.path == path,
                )
                .first()
            )

            absolute_path = regular_source_path(repository_path, path)
            if absolute_path is None:
                if code_file is not None:
                    db.execute(delete(CodeSymbol).where(CodeSymbol.file_id == code_file.id))
                    db.delete(code_file)
                    files_deleted += 1
                continue

            if code_file is None:
                code_file = CodeFile(
                    repository_id=repository.id,
                    path=path,
                )

                db.add(code_file)
                db.flush()

                if status != "R":
                    files_added += 1

            else:
                if status == "M":
                    files_modified += 1

            code_file.language = language
            code_file.content_hash = (
                calculate_file_hash(
                    absolute_path
                )
            )
            code_file.last_indexed_commit = (
                new_commit
            )

            # Remove old AST symbols.
            db.execute(
                delete(CodeSymbol).where(
                    CodeSymbol.file_id
                    == code_file.id
                )
            )

            # Currently Tree-sitter support is
            # implemented for Python.
            if language == "python":
                source_code = (
                    absolute_path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                )

                symbols = parse_python_source(
                    source_code
                )

                for symbol in symbols:
                    db.add(
                        CodeSymbol(
                            repository_id=repository.id,
                            file_id=code_file.id,
                            name=symbol.name,
                            qualified_name=(
                                symbol.qualified_name
                            ),
                            kind=symbol.kind,
                            start_line=(
                                symbol.start_line
                            ),
                            end_line=(
                                symbol.end_line
                            ),
                            code=symbol.code,
                        )
                    )

                files_parsed += 1

            updated_file_ids.append(
                code_file.id
            )

        job = RepositorySyncJob(
            repository_id=repository.id,
            old_commit=old_commit,
            target_commit=new_commit,
            affected_paths=sorted(affected_paths),
            file_ids=updated_file_ids,
            stats={
                "files_added": files_added,
                "files_modified": files_modified,
                "files_deleted": files_deleted,
                "files_renamed": files_renamed,
                "files_parsed": files_parsed,
            },
        )
        db.add(job)
        db.commit()

    except Exception:
        db.rollback()
        raise

    return _finish_sync(db, repository, job, resumed=False)
