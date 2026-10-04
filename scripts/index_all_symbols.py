import argparse
from app.db.database import SessionLocal
from app.db.models import Repository
from app.indexer.symbols import index_repository_symbols
from scripts.arguments import run_indexing_command


def main():
    db = SessionLocal()

    try:
        repositories = (
            db.query(Repository)
            .order_by(Repository.id)
            .all()
        )

        print(
            f"Repositories found: {len(repositories)}"
        )
        print()

        total_symbols = 0

        failures = 0

        for repository in repositories:
            print("=" * 70)
            print(
                f"Processing repository "
                f"{repository.id}: {repository.name}"
            )

            try:
                result = index_repository_symbols(
                    db=db,
                    repository_id=repository.id,
                )

                symbol_count = result[
                    "symbols_indexed"
                ]

                total_symbols += symbol_count

                print(f"Publication pending; next: .venv/bin/python -m scripts.index_elasticsearch --repository-id {repository.id}")

                print(
                    f"{repository.name:<20} "
                    f"{symbol_count:>6} symbols"
                )

                print(
                    f"Parsed files: "
                    f"{result['parsed_files']}"
                )

                print(
                    f"Skipped files: "
                    f"{result['skipped_files']}"
                )

            except Exception as exc:
                failures += 1
                db.rollback()

                print(
                    f"FAILED: {repository.name}"
                )

                print(
                    f"{type(exc).__name__}: Indexing failed; inspect pending recovery work."
                )

            print()

        print("=" * 70)
        print(
            f"Total symbols indexed: "
            f"{total_symbols}"
        )

        if failures:
            raise SystemExit(f"{failures} repositories failed; retry pending work.")

    finally:
        db.close()


def cli(argv=None):
    argparse.ArgumentParser(description="Prepare symbol snapshots for all registered repositories.").parse_args(argv)
    return run_indexing_command(main)


if __name__ == "__main__":
    raise SystemExit(cli())
