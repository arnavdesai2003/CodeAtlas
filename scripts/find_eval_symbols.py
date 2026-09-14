from app.db.database import SessionLocal
from app.db.models import CodeFile, CodeSymbol, Repository


SEARCH_TERMS = [
    "request",
    "response",
    "client",
    "session",
    "cookie",
    "header",
    "url",
    "redirect",
    "command",
    "option",
    "argument",
    "parse",
    "token",
    "sign",
    "verify",
    "escape",
    "markup",
    "auth",
    "timeout",
    "transport",
    "gradient",
    "backward",
    "layer",
]


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
            print("=" * 100)
            print(repository.name.upper())
            print("=" * 100)

            symbols = (
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
                .all()
            )

            matches = []

            for symbol, code_file in symbols:
                searchable = (
                    f"{symbol.qualified_name} "
                    f"{symbol.name} "
                    f"{code_file.path}"
                ).lower()

                if any(
                    term in searchable
                    for term in SEARCH_TERMS
                ):
                    matches.append(
                        (
                            symbol,
                            code_file,
                        )
                    )

            for symbol, code_file in matches[:40]:
                print(
                    f"{symbol.kind:<10} "
                    f"{symbol.qualified_name:<50} "
                    f"{code_file.path}"
                )

    finally:
        db.close()


if __name__ == "__main__":
    main()