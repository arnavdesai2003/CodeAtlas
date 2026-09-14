from app.db.database import SessionLocal
from app.indexer.repository import ingest_repository


REPOSITORIES = [
    "https://github.com/pallets/click.git",
    "https://github.com/pallets/itsdangerous.git",
    "https://github.com/pallets/markupsafe.git",
    "https://github.com/psf/requests.git",
    "https://github.com/encode/httpx.git",
]


def main():
    db = SessionLocal()

    try:
        for clone_url in REPOSITORIES:
            print()
            print("=" * 70)
            print(f"Ingesting: {clone_url}")

            try:
                result = ingest_repository(
                    db=db,
                    clone_url=clone_url,
                )

                print(
                    f"Repository ID: "
                    f"{result['repository_id']}"
                )

                print(
                    f"Files discovered: "
                    f"{result['source_files_discovered']}"
                )

            except ValueError as exc:
                print(f"Skipped: {exc}")

            except Exception as exc:
                print(
                    f"Failed: "
                    f"{type(exc).__name__}: {exc}"
                )

    finally:
        db.close()


if __name__ == "__main__":
    main()