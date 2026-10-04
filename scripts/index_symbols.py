import argparse
from app.db.database import SessionLocal
from app.indexer.locking import validate_repository_id
from scripts.arguments import repository_id_argument, run_indexing_command
from app.indexer.symbols import index_repository_symbols


def main(repository_id: int = 1):
    validate_repository_id(repository_id)
    db = SessionLocal()

    try:
        result = index_repository_symbols(
            db=db,
            repository_id=repository_id,
        )

        print("Symbol indexing completed")
        print()

        for key, value in result.items():
            print(f"{key}: {value}")

    finally:
        db.close()


def cli(argv=None):
    parser = argparse.ArgumentParser(description="Build or resume symbols for one repository.")
    parser.add_argument("--repository-id", type=repository_id_argument, default=1)
    return run_indexing_command(main, parser.parse_args(argv).repository_id)


if __name__ == "__main__":
    raise SystemExit(cli())
