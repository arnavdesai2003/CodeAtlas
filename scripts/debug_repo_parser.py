from pathlib import Path

from app.db.database import SessionLocal
from app.db.models import CodeFile, Repository
from app.indexer.parser import parse_python_source
from app.indexer.repository import REPOSITORY_ROOT, parse_github_url


REPOSITORY_ID = 2


def main():
    db = SessionLocal()

    try:
        repository = db.get(Repository, REPOSITORY_ID)

        if repository is None:
            print(
                f"Repository {REPOSITORY_ID} not found.",
                flush=True,
            )
            return

        owner, name = parse_github_url(
            repository.clone_url
        )

        repo_path = (
            REPOSITORY_ROOT
            / owner
            / name
        )

        files = (
            db.query(CodeFile)
            .filter(
                CodeFile.repository_id
                == REPOSITORY_ID,
                CodeFile.language == "python",
            )
            .order_by(CodeFile.id)
            .all()
        )

        print(
            f"Repository: {repository.name}",
            flush=True,
        )

        print(
            f"Python files: {len(files)}",
            flush=True,
        )

        for i, code_file in enumerate(
            files,
            start=1,
        ):
            path = repo_path / Path(
                code_file.path
            )

            print(
                f"\n[{i}/{len(files)}] "
                f"START {code_file.path}",
                flush=True,
            )

            if not path.exists():
                print(
                    "    FILE MISSING",
                    flush=True,
                )
                continue

            source = path.read_text(
                encoding="utf-8",
                errors="replace",
            )

            print(
                f"    bytes="
                f"{len(source.encode('utf-8'))}",
                flush=True,
            )

            symbols = parse_python_source(
                source
            )

            print(
                f"    OK symbols="
                f"{len(symbols)}",
                flush=True,
            )

    finally:
        db.close()


if __name__ == "__main__":
    main()