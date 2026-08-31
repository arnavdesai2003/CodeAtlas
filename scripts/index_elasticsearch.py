from app.db.database import SessionLocal
from app.search.engine import (
    index_repository_in_elasticsearch,
)


def main():
    db = SessionLocal()

    try:
        result = index_repository_in_elasticsearch(
            db=db,
            repository_id=1,
        )

        print("Elasticsearch indexing completed")
        print()

        for key, value in result.items():
            print(f"{key}: {value}")

    finally:
        db.close()


if __name__ == "__main__":
    main()