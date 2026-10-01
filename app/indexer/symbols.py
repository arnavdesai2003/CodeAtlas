from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import (
    CodeFile, CodeSymbol, Repository, RepositorySyncJob, RepositoryFullIndexJob,
)
from app.indexer.locking import repository_sync_lock
from app.indexer.parser import parse_python_source
from app.indexer.repository import REPOSITORY_ROOT, parse_github_url


def index_repository_symbols(
    db: Session,
    repository_id: int,
) -> dict:
    try:
        with repository_sync_lock(db, repository_id):
            return _index_repository_symbols(db, repository_id)
    except Exception:
        db.rollback()
        raise


def _index_repository_symbols(db: Session, repository_id: int) -> dict:
    if db.get(RepositorySyncJob, repository_id) is not None:
        raise RuntimeError("Finish pending repository synchronization before full symbol indexing.")
    pending = db.get(RepositoryFullIndexJob, repository_id)
    if pending is not None:
        if not pending.stats:
            raise RuntimeError("Finish pending full Elasticsearch indexing before replacing symbols.")
        return {**pending.stats, "publication_pending": True, "resumed": True}

    repository = db.get(Repository, repository_id)

    if repository is None:
        raise ValueError(
            f"Repository {repository_id} does not exist."
        )

    owner, repository_name = parse_github_url(
        repository.clone_url
    )

    repository_path = (
        REPOSITORY_ROOT
        / owner
        / repository_name
    )

    if not repository_path.exists():
        raise FileNotFoundError(
            f"Repository directory not found: {repository_path}"
        )

    files = (
        db.query(CodeFile)
        .filter(CodeFile.repository_id == repository_id)
        .all()
    )

    total_symbols = 0
    parsed_files = 0
    skipped_files = 0

    # Replace the entire repository snapshot, including missing/non-Python files.
    db.execute(delete(CodeSymbol).where(CodeSymbol.repository_id == repository_id))

    for code_file in files:
        if code_file.language != "python":
            skipped_files += 1
            continue

        file_path = repository_path / Path(code_file.path)

        if not file_path.exists():
            skipped_files += 1
            continue

        source_code = file_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        symbols = parse_python_source(source_code)

        for symbol in symbols:
            db.add(
                CodeSymbol(
                    repository_id=repository.id,
                    file_id=code_file.id,
                    name=symbol.name,
                    qualified_name=symbol.qualified_name,
                    kind=symbol.kind,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    code=symbol.code,
                )
            )

        total_symbols += len(symbols)
        parsed_files += 1

    result = {
        "repository_id": repository.id,
        "repository": repository.name,
        "parsed_files": parsed_files,
        "skipped_files": skipped_files,
        "symbols_indexed": total_symbols,
    }

    db.add(RepositoryFullIndexJob(repository_id=repository_id, stats=result))
    db.commit()
    return {**result, "publication_pending": True, "resumed": False}
