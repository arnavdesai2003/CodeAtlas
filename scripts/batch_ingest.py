import argparse
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
        failures = 0
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

            except Exception as exc:
                failures += 1
                db.rollback()
                print(
                    f"Failed: "
                    f"{type(exc).__name__}: Inspect metadata and clone state before retrying."
                )
        if failures:
            raise SystemExit(f"{failures} repositories failed; inspect metadata and clone state before retrying.")

    finally:
        db.close()


def cli(argv=None):
    argparse.ArgumentParser(description="Ingest the five configured corpus repositories.").parse_args(argv)
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: Ingestion failed; inspect metadata and clone state before retrying.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
