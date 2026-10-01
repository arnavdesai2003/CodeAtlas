import argparse

from app.db.database import SessionLocal
from app.search.engine import (
    index_repository_in_elasticsearch,
)


def main(repository_id: int = 1):
    db = SessionLocal()

    try:
        result = index_repository_in_elasticsearch(
            db=db,
            repository_id=repository_id,
        )

        print("Elasticsearch indexing completed")
        print()

        for key, value in result.items():
            print(f"{key}: {value}")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build or resume atomic publication for one repository.")
    parser.add_argument("--repository-id", type=int, default=1)
    main(parser.parse_args().repository_id)
