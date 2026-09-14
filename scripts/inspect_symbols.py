from app.db.database import SessionLocal
from app.db.models import CodeFile, CodeSymbol, Repository


SYMBOLS_PER_REPOSITORY = 30


def main():
    db = SessionLocal()

    try:
        repositories = (
            db.query(Repository)
            .order_by(Repository.id)
            .all()
        )

        for repository in repositories:
            print()
            print("=" * 90)
            print(
                f"Repository {repository.id}: "
                f"{repository.name}"
            )
            print("=" * 90)

            rows = (
                db.query(
                    CodeSymbol,
                    CodeFile,
                )
                .join(
                    CodeFile,
                    CodeSymbol.file_id
                    == CodeFile.id,
                )
                .filter(
                    CodeSymbol.repository_id
                    == repository.id
                )
                .order_by(
                    CodeSymbol.kind,
                    CodeSymbol.qualified_name,
                )
                .limit(
                    SYMBOLS_PER_REPOSITORY
                )
                .all()
            )

            for symbol, code_file in rows:
                print(
                    f"{symbol.kind:<10} "
                    f"{symbol.qualified_name:<45} "
                    f"{code_file.path}"
                )

    finally:
        db.close()


if __name__ == "__main__":
    main()