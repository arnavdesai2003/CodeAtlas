from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import CodeFile, CodeSymbol, Repository, RepositorySyncJob
from app.indexer.parser import parse_python_source
from app.indexer.repository import REPOSITORY_ROOT, parse_github_url


def index_repository_symbols(
    db: Session,
    repository_id: int,
) -> dict:
    if db.get(RepositorySyncJob, repository_id) is not None:
        raise RuntimeError("Finish pending repository synchronization before full symbol indexing.")

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

        # Delete previous symbols so re-indexing is idempotent.
        db.execute(
            delete(CodeSymbol).where(
                CodeSymbol.file_id == code_file.id
            )
        )

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

    db.commit()

    return {
        "repository_id": repository.id,
        "repository": repository.name,
        "parsed_files": parsed_files,
        "skipped_files": skipped_files,
        "symbols_indexed": total_symbols,
    }
