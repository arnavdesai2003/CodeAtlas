import argparse
from app.db.database import SessionLocal
from app.db.models import Repository
from app.search.engine import index_repository_in_elasticsearch
from scripts.arguments import run_indexing_command


def main():
    db = SessionLocal()

    try:
        repositories = (
            db.query(Repository)
            .order_by(Repository.id)
            .all()
        )

        print(f"Repositories found: {len(repositories)}")
        print()

        total_documents = 0

        failures = 0

        for repository in repositories:
            print("=" * 70)
            print(
                f"Indexing repository "
                f"{repository.id}: {repository.name}"
            )

            try:
                result = (
                    index_repository_in_elasticsearch(
                        db=db,
                        repository_id=repository.id,
                    )
                )

                count = result["symbols_indexed"]

                total_documents += count

                print(
                    f"{repository.name:<20} "
                    f"{count:>6} documents indexed"
                )

            except Exception as exc:
                failures += 1
                print(
                    f"FAILED: {repository.name}"
                )

                print(
                    f"{type(exc).__name__}: Indexing failed; inspect pending recovery work."
                )

            print()

        print("=" * 70)
        print(
            f"Total Elasticsearch documents indexed: "
            f"{total_documents}"
        )

        if failures:
            raise SystemExit(f"{failures} repositories failed; retry pending work.")

    finally:
        db.close()


def cli(argv=None):
    argparse.ArgumentParser(description="Build or resume Elasticsearch publication for all registered repositories.").parse_args(argv)
    return run_indexing_command(main)


if __name__ == "__main__":
    raise SystemExit(cli())
